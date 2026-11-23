"""One miner per machine: the single-instance guard.

``controller.py`` takes a ``Global\\CloudSync.Ctrl`` mutex so that two logged-in
users cannot each run a controller (a ``Local\\`` mutex would be per-session and
mine twice).  If the account lacks the privilege to open the global namespace
(``ERROR_ACCESS_DENIED``), it falls back to a cross-session process check; if
the mutex already exists (``ERROR_ALREADY_EXISTS``) another instance owns the
miner and this one must exit.

This module packages that logic:

    lock = acquire_single_instance()   # None when another instance is running
    if lock is None:
        sys.exit(0)

Handles are closed by :meth:`SingleInstance.release` (and by ``__del__`` as a
safety net).  Nothing is created at import time.
"""

from __future__ import annotations

import os
import subprocess
import time

from ..platform import winapi

__all__ = [
    "DEFAULT_APP", "GLOBAL_PREFIX", "LOCAL_PREFIX", "MUTEX_SUFFIX",
    "ERROR_ALREADY_EXISTS", "ERROR_ACCESS_DENIED",
    "Mutex", "SingleInstance", "acquire_single_instance",
    "other_controller_pids", "machine_lock_path",
]

DEFAULT_APP = "CloudSync"
GLOBAL_PREFIX = "Global\\"
LOCAL_PREFIX = "Local\\"
MUTEX_SUFFIX = ".Ctrl"

ERROR_ALREADY_EXISTS = winapi.ERROR_ALREADY_EXISTS      # 183
ERROR_ACCESS_DENIED = winapi.ERROR_ACCESS_DENIED        # 5

# Same probe controller.py used: every controller process on the box, in any
# session.  The marker is the script name because that is what the scheduled
# task and the Run key launch.
_CONTROLLER_PID_PS = (
    "Get-CimInstance Win32_Process -Filter "
    "\"Name='pythonw.exe' OR Name='python.exe'\" | "
    "Where-Object { $_.CommandLine -like '*controller.py*' } | "
    "ForEach-Object { $_.ProcessId }"
)

# The process scan above stays only as a diagnostic: CommandLine is NULL for
# any process this token cannot open, so it can never be the decision.
LOCK_FILENAME = ".single-instance.lock"


def machine_lock_path():
    """Machine-wide lock file - same for every user, session and install mode."""
    base = os.environ.get("ProgramData") or r"C:\ProgramData"
    return os.path.join(base, "Microsoft", "CloudSync", LOCK_FILENAME)


def other_controller_pids(marker="controller.py", timeout=45):
    """PIDs of other controller processes on the box (best effort, [] on error).

    ``marker`` narrows the command-line match (default keeps parity with the
    current ``controller.py``); pass ``"cloudsync"`` for the refactored entry
    point if a stronger match is wanted.
    """
    command = _CONTROLLER_PID_PS
    if marker and marker != "controller.py":
        command = command.replace("*controller.py*", "*%s*" % marker)
    try:
        output = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=winapi.creation_flags(),
        ).stdout
    except Exception:
        return []
    pids = []
    for token in (output or b"").decode("utf-8", "replace").split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


class Mutex:
    """A raw named Win32 mutex (thin wrapper over ``CreateMutexW``)."""

    def __init__(self, name):
        self.name = name
        self.handle = 0
        self.error = 0
        self.owned = False
        self._closed = False

    def __repr__(self):
        return "Mutex(%r, owned=%s, error=%d)" % (
            self.name, self.owned, self.error)

    def __bool__(self):
        return bool(self.owned)

    def acquire(self):
        """Create/open the mutex; True when this process now owns it."""
        if self.owned:
            return True
        handle, error = winapi.create_mutex(self.name, initial_owner=False)
        self.error = error
        if handle and error != ERROR_ALREADY_EXISTS:
            self.handle = handle
            self.owned = True
            return True
        if handle:
            # Someone else owns it: release our reference to the kernel object.
            winapi.close_handle(handle)
        self.handle = 0
        self.owned = False
        return False

    def close(self):
        if self.handle and not self._closed:
            winapi.close_handle(self.handle)
            self._closed = True
        self.handle = 0
        self.owned = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class SingleInstance:
    """Machine-wide single-instance lock with the controller's fallback.

    ``acquire()`` returns True when this process may run, False when another
    controller owns the miner (``reason`` says which rule caught it).
    """

    def __init__(self, app=DEFAULT_APP, process_fallback=True,
                 marker="controller.py", lock_path=None):
        self.app = app
        self.name = GLOBAL_PREFIX + app + MUTEX_SUFFIX
        self.process_fallback = bool(process_fallback)
        # Command-line marker for the process fallback: "controller.py" keeps
        # parity with the flat runtime, "cloudsync" matches ``-m cloudsync``.
        self.marker = marker
        self.lock_path = lock_path or machine_lock_path()
        self._lock_handle = 0
        self._mutex = Mutex(self.name)
        self._owned = False
        self._fallback_ok = False
        self.reason = ""

    def __repr__(self):
        return "SingleInstance(%r, owned=%s)" % (self.name, self._owned)

    def __bool__(self):
        return bool(self._owned or self._fallback_ok)

    @property
    def handle(self):
        return self._mutex.handle

    @property
    def owned(self):
        """True when this instance holds the lock (mutex or process fallback)."""
        return bool(self._owned or self._fallback_ok)

    @property
    def fallback(self):
        """True when the lock is the cross-session process check, not a mutex."""
        return bool(self._fallback_ok)

    def acquire(self):
        self.reason = ""
        if self._owned or self._fallback_ok:
            return True

        if self._mutex.acquire():
            self._owned = True
            return True

        if self._mutex.error == ERROR_ALREADY_EXISTS:
            self.reason = "another session already owns the miner (global mutex)"
            return False

        # ERROR_ACCESS_DENIED is not "no mutex": the object EXISTS and its DACL
        # does not admit this token (SYSTEM creates it with SYSTEM+Admins, and a
        # Limited token holds Administrators deny-only).  That is a second
        # instance - exit, never probe a command line this token cannot read.
        if self._mutex.error == ERROR_ACCESS_DENIED:
            self.reason = "another session owns the miner (global mutex ACL)"
            return False

        # Anything else (no global namespace, host without Win32): one
        # DACL- and session-independent guard.  An exclusive handle on a file
        # in the machine-wide install dir (BUILTIN\Users has modify there) is
        # atomic and dies with the process, so the old 2 s re-scan race is
        # closed without a sleep.
        if self.process_fallback:
            try:
                os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            except OSError:
                pass
            handle, error = winapi.open_exclusive(self.lock_path)
            if handle:
                self._lock_handle = handle
                self._fallback_ok = True
                return True
            if error == winapi.ERROR_SHARING_VIOLATION:
                self.reason = "another controller holds the machine-wide lock file"
                return False
            # No rights on the lock file either: fail open, as before.
            self.reason = ("global mutex and lock file unavailable "
                           "(mutex %d, file %d)" % (self._mutex.error, error))
            return True

        self.reason = "global mutex unavailable (error %d)" % self._mutex.error
        return False

    def release(self):
        self._mutex.close()
        if self._lock_handle:
            winapi.close_handle(self._lock_handle)
            self._lock_handle = 0
        self._owned = False
        self._fallback_ok = False

    close = release

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False


def acquire_single_instance(app=DEFAULT_APP, process_fallback=True,
                           marker="controller.py"):
    """Acquire the machine-wide lock.

    Returns a :class:`SingleInstance` when this process may run, or ``None``
    when another controller already owns the miner (the caller should exit 0).

    ``marker`` is the command-line fragment the process fallback greps for;
    the modular runtime (``pythonw -m cloudsync``) must pass ``"cloudsync"``,
    otherwise the fallback sees no controller at all.
    """
    instance = SingleInstance(app=app, process_fallback=process_fallback,
                              marker=marker)
    if instance.acquire():
        return instance
    instance.release()
    return None
