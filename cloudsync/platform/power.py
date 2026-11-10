"""Power management: keep the machine awake, inspect / set the power scheme.

Two separate concerns live here:

* **Execution state** - ``SetThreadExecutionState`` is how the controller stops
  the box sleeping while the worker is mining.  The old code called it with
  ``ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)``; :func:`keep_system_awake`
  is that exact call, and :func:`allow_sleep` releases it (called on exit).
* **Power scheme** - read-only helper for the active scheme GUID and a
  best-effort ``powercfg /setactive``.  The Windows installer owns baseline
  capture/restore; these helpers exist so Python code can check (and, when
  elevated, change) the scheme without shelling out ad hoc.

Every call degrades gracefully: unavailable APIs return False / None, and
subprocess timeouts are swallowed.  Nothing runs at import time.
"""

from __future__ import annotations

import re
import subprocess

from ..platform import winapi

__all__ = [
    "ES_CONTINUOUS", "ES_SYSTEM_REQUIRED", "ES_DISPLAY_REQUIRED",
    "keep_system_awake", "allow_sleep", "set_thread_execution_state",
    "active_scheme", "set_active_scheme", "available",
]

ES_CONTINUOUS = winapi.ES_CONTINUOUS
ES_SYSTEM_REQUIRED = winapi.ES_SYSTEM_REQUIRED
ES_DISPLAY_REQUIRED = winapi.ES_DISPLAY_REQUIRED

_GUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def set_thread_execution_state(flags):
    """Raw SetThreadExecutionState(flags); False when unavailable."""
    return winapi.set_thread_execution_state(int(flags))


def keep_system_awake(enabled):
    """Keep the system awake while ``enabled`` is True; release otherwise.

    Mirrors the old ``keep_system_awake`` exactly: ``ES_CONTINUOUS`` plus
    ``ES_SYSTEM_REQUIRED`` when the worker is running and wanted.
    """
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
    return set_thread_execution_state(flags)


def allow_sleep():
    """Release the continuous execution-state request (same as disabled)."""
    return keep_system_awake(False)


# ---------------------------------------------------------------------------
# power scheme helpers (powercfg.exe)
# ---------------------------------------------------------------------------


def _powercfg(args, timeout=30):
    """Run powercfg with no window; returns CompletedProcess or None."""
    try:
        return subprocess.run(
            ["powercfg.exe"] + list(args),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=winapi.creation_flags(),
        )
    except Exception:
        return None


def active_scheme(timeout=30):
    """GUID of the active power scheme (lower case), or None."""
    result = _powercfg(["/getactivescheme"], timeout=timeout)
    if result is None:
        return None
    text = (result.stdout or b"").decode("utf-8", "replace")
    match = _GUID_RE.search(text)
    return match.group(1).lower() if match else None


def set_active_scheme(guid, timeout=30):
    """Activate a power scheme by GUID (needs elevation); True on success."""
    if not guid:
        return False
    result = _powercfg(["/setactive", str(guid)], timeout=timeout)
    return bool(result is not None and result.returncode == 0)


def available():
    """True when powercfg.exe is on PATH (Windows installs)."""
    try:
        from shutil import which
        return which("powercfg.exe") is not None
    except Exception:
        return False
