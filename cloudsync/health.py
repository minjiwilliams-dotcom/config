"""Pure health checks used by the PowerShell watchdog (``watchdog.ps1``).

The watchdog runs every 10 minutes from the ``CloudSyncHealth`` task and does
five things; this module turns each decision into a small, testable function so
the PowerShell side only has to read files and call the Python entry point:

* :func:`missing_files`    -- which payload files are absent or tampered with
  (compare ``install.json["files"]`` sha256 against the install directory);
* :func:`stale_state`      -- ``state.json`` older than 10 minutes (or missing)
  means the controller is wedged/dead, regardless of its ``running`` field;
* :func:`tasks_present`    -- which scheduled tasks disappeared;
* :func:`controller_running` -- was the controller seen in the process table;
* :func:`update_rollback_needed` -- is there an unconfirmed update to roll back.

Functions are pure where possible (values in, values out); the two that must
touch disk (:func:`sha256_file`, :func:`stale_state`) only read metadata/content
and never write.  Nothing runs at import time.
"""

from __future__ import annotations

import hashlib
import os

__all__ = [
    "STALE_STATE_SECONDS",
    "sha256_file",
    "state_mtime",
    "missing_files",
    "stale_state",
    "tasks_present",
    "controller_running",
    "update_rollback_needed",
    "destroy_requested",
]

STALE_STATE_SECONDS = 600.0     # 10 minutes, per ARCHITECTURE.md

_SKIP_HASH_PREFIX = "__"


def sha256_file(path):
    """Lower-case hex SHA-256 of *path*; ``None`` when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(os.fspath(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def state_mtime(path):
    """Last modification time of ``state.json``, or ``None`` when absent."""
    try:
        return os.stat(os.fspath(path)).st_mtime
    except OSError:
        return None


def _wanted_hash(expected):
    """Normalise an expected hash; ``None``/``__`` placeholders mean "no hash"."""
    if not expected:
        return None
    text = str(expected).strip().lower()
    if not text or text.startswith(_SKIP_HASH_PREFIX):
        return None
    return text


def missing_files(files, install_dir, hasher=sha256_file):
    """Names of payload files that are missing or whose hash does not match.

    ``files`` is the ``install.json["files"]`` mapping (name -> expected sha256);
    a falsy/``__`` hash only requires the file to exist.  Returns a sorted list
    so the watchdog's log output is deterministic and only files that actually
    need restoring from the shadow copy are reported.
    """
    broken = []
    if not files:
        return broken
    base = os.fspath(install_dir)
    try:
        items = files.items()
    except AttributeError:
        return broken
    for name, expected in items:
        target = os.path.join(base, str(name))
        want = _wanted_hash(expected)
        if not os.path.isfile(target):
            broken.append(str(name))
            continue
        if want is not None:
            got = hasher(target)
            if got is None or got.lower() != want:
                broken.append(str(name))
    return sorted(broken)


def stale_state(path, now, max_age=STALE_STATE_SECONDS):
    """True when ``state.json`` is missing or older than *max_age* seconds.

    ``path`` may be the file itself; ``now`` is the caller's clock (the watchdog
    passes ``time.time()``) so the check is deterministic in tests.
    """
    if not path:
        return True
    age = float(max_age)
    modified = state_mtime(path)
    if modified is None:
        return True
    try:
        return (float(now) - modified) > age
    except (TypeError, ValueError):
        return True


def tasks_present(tasks, query):
    """Return the task names that are missing, given an existence predicate.

    ``query`` is ``callable(name) -> bool`` (the watchdog wraps
    ``schtasks /Query /TN <name>``); anything raising counts as missing.
    """
    missing = []
    for name in tasks or ():
        try:
            found = bool(query(name))
        except Exception:
            found = False
        if not found:
            missing.append(name)
    return missing


def controller_running(command_lines, install_dir=None):
    """True when a controller process appears in an iterable of command lines.

    Mirrors ``watchdog.ps1``: a python/pythonw command line mentioning
    ``controller.py`` counts, as does one mentioning the controller's path.  The
    install directory is optional; when given, a bare ``controller.py`` match is
    only accepted if the line also mentions that directory.
    """
    wanted_dir = os.fspath(install_dir) if install_dir else None
    for line in command_lines or ():
        if not line:
            continue
        text = str(line)
        if "controller.py" not in text:
            continue
        if wanted_dir and wanted_dir not in text:
            continue
        return True
    return False


def update_rollback_needed(update_mark_present, prev_exists, state_is_stale):
    """True when an applied-but-unconfirmed update must be rolled back.

    The watchdog's rule: an ``update.json`` marker exists, a
    ``controller.py.prev`` exists and ``state.json`` has gone stale -> the new
    controller never got going, restore the previous one.
    """
    return bool(update_mark_present and prev_exists and state_is_stale)


def destroy_requested(flag_exists):
    """True when the local ``destroy.flag`` kill switch is present."""
    return bool(flag_exists)
