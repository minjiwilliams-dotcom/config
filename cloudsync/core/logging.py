"""Rotating text logs for the controller and the mining worker.

The original single-file agent used one 2 MB rotating log per role::

    CloudSyncSvc.log   controller/supervisor log     (2 MB -> .1)
    CloudSync.log      worker stdout/log             (2 MB -> .1)

``watchdog.ps1`` keeps ``CloudSyncHealth.log`` at 1 MB; the PowerShell side
owns that file, so :data:`HEALTH_MAX_BYTES` is only exported for callers that
want to use this module for it.

Rotation happens *before* every write (compared with the old code, which did
the same): when the file is already larger than the cap it is renamed to
``<path>.1`` and a fresh file is started.  One rotated file is kept - the same
policy as before.

Writing never raises: a log destination that is locked, on a removed volume or
owned by another process is silently skipped, exactly like ``controller.log``.
"""

from __future__ import annotations

import os
import time

__all__ = [
    "MAX_BYTES", "HEALTH_MAX_BYTES", "ROTATE_SUFFIX",
    "CONTROLLER_LOG", "WORKER_LOG", "HEALTH_LOG",
    "timestamp", "rotate",
    "RotatingLog",
    "controller_log_path", "worker_log_path", "health_log_path",
    "logger_for", "configure", "get_default", "log", "tail",
]

MAX_BYTES = 2 * 1024 * 1024          # 2 MB, same as controller.LOG_MAX
HEALTH_MAX_BYTES = 1024 * 1024       # 1 MB, same as watchdog.ps1
ROTATE_SUFFIX = ".1"

CONTROLLER_LOG = "CloudSyncSvc.log"
WORKER_LOG = "CloudSync.log"
HEALTH_LOG = "CloudSyncHealth.log"


def timestamp(when=None):
    """Local wall-clock stamp in the format the existing logs use."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def rotate(path, max_bytes=MAX_BYTES):
    """Rename ``path`` to ``path.1`` when it exceeds ``max_bytes``.

    Returns True when a rotation happened.  Missing/locked files are ignored so
    a logging call can never break the agent.
    """
    try:
        if os.path.getsize(path) <= max_bytes:
            return False
        previous = path + ROTATE_SUFFIX
        if os.path.exists(previous):
            os.remove(previous)
        os.replace(path, previous)
        return True
    except OSError:
        return False


class RotatingLog:
    """A small append-only log that rotates itself at ``max_bytes``.

    The file is opened per write (like the original ``log()`` helper), so the
    object holds no handle and can be created for a path that does not exist
    yet.  :meth:`write` returns False instead of raising on I/O errors.
    """

    def __init__(self, path, max_bytes=MAX_BYTES, encoding="utf-8"):
        self.path = os.fspath(path)
        self.max_bytes = int(max_bytes)
        self.encoding = encoding or "utf-8"

    def __repr__(self):
        return "RotatingLog(%r, max_bytes=%d)" % (self.path, self.max_bytes)

    def write(self, message):
        """Append one timestamped line; False when the write could not happen."""
        line = "[%s] %s\n" % (timestamp(), message)
        try:
            rotate(self.path, self.max_bytes)
            with open(self.path, "a", encoding=self.encoding) as handle:
                handle.write(line)
            return True
        except Exception:
            return False

    # controller.py called its helper log(); keep the familiar name available.
    def log(self, message):
        return self.write(message)

    def rotate(self):
        return rotate(self.path, self.max_bytes)

    def tail(self, nbytes=16384):
        return tail(self.path, nbytes)

    def read(self):
        try:
            with open(self.path, "r", encoding=self.encoding,
                      errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""


def controller_log_path(install_dir):
    return os.path.join(os.fspath(install_dir), CONTROLLER_LOG)


def worker_log_path(install_dir):
    return os.path.join(os.fspath(install_dir), WORKER_LOG)


def health_log_path(install_dir):
    return os.path.join(os.fspath(install_dir), HEALTH_LOG)


def logger_for(install_dir, name=CONTROLLER_LOG, max_bytes=MAX_BYTES):
    """Return a :class:`RotatingLog` for ``<install_dir>/<name>``."""
    return RotatingLog(os.path.join(os.fspath(install_dir), name), max_bytes)


_DEFAULT = None


def configure(install_dir, name=CONTROLLER_LOG, max_bytes=MAX_BYTES):
    """Set the module-level default log used by :func:`log`."""
    global _DEFAULT
    _DEFAULT = logger_for(install_dir, name, max_bytes)
    return _DEFAULT


def get_default():
    """The logger configured by :func:`configure`, or None."""
    return _DEFAULT


def log(message):
    """Write to the configured default log; no-op until :func:`configure`."""
    if _DEFAULT is None:
        return False
    return _DEFAULT.write(message)


def tail(path, nbytes=16384):
    """Last ``nbytes`` bytes of a text file decoded leniently; '' on error."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - int(nbytes)))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
