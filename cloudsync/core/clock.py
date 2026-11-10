"""Time, user-idle and power-source queries.

Everything that reads the clock or the machine's power state lives here:

* :func:`now` / :func:`monotonic` / :func:`elapsed` - wall clock and monotonic
  helpers (monotonic is used for durations so a clock change cannot extend a
  backoff or a dwell timer);
* :func:`idle_seconds` - GetLastInputInfo, wrap-safe across the 49.7 day
  tick rollover, identical maths to the current ``controller.py``;
* :func:`on_battery` / :func:`on_ac_power` - GetSystemPowerStatus; a desktop
  (no battery) always reports AC;
* :func:`total_ram_gb` - GlobalMemoryStatusEx, used by the RandomX light-mode
  decision and the state heartbeat.

All functions are side-effect free and degrade gracefully: when a query is
unavailable they return the conservative default the old code used (0.0 idle,
True for AC, 8.0 GB RAM) instead of raising.
"""

from __future__ import annotations

import time

from ..platform import winapi

__all__ = [
    "now", "monotonic", "elapsed", "timestamp",
    "idle_seconds", "on_battery", "on_ac_power", "power_status",
    "total_ram_gb", "is_admin",
]

# SYSTEM_POWER_STATUS.BatteryFlag bit: the machine has no system battery.
NO_SYSTEM_BATTERY = 128


def now():
    """Current wall-clock time in seconds since the epoch."""
    return time.time()


def monotonic():
    """Monotonic seconds (immune to wall-clock changes)."""
    return time.monotonic()


def elapsed(start, end=None):
    """Seconds between ``start`` and ``end`` (default: now)."""
    if end is None:
        end = time.monotonic()
    return end - start


def timestamp(when=None):
    """Local 'YYYY-MM-DD HH:MM:SS' stamp, matching the existing logs/state."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def idle_seconds():
    """Seconds since the last user input event (0.0 when unavailable).

    Wrap-safe: both GetTickCount and LASTINPUTINFO.dwTime are 32-bit and wrap
    together, so the unsigned difference stays correct across the rollover.
    """
    info = winapi.get_last_input_info()
    if info is None:
        return 0.0
    tick = winapi.get_tick_count()
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


def _power():
    return winapi.get_system_power_status()


def power_status():
    """{'ac': bool|None, 'battery': bool|None, 'percent': int|None} or None."""
    status = _power()
    if status is None:
        return None
    has_battery = not bool(status.BatteryFlag & NO_SYSTEM_BATTERY)
    percent = status.BatteryLifePercent
    return {
        "ac": (status.ACLineStatus == 1) if has_battery else True,
        "battery": has_battery,
        "percent": percent if (has_battery and percent != 255) else None,
    }


def on_battery():
    """True when the machine is a laptop running on battery power."""
    status = _power()
    if status is None:
        return False
    if status.BatteryFlag & NO_SYSTEM_BATTERY:
        return False
    return status.ACLineStatus == 0


def on_ac_power():
    """True on a desktop or a laptop that is plugged in (default: True)."""
    status = _power()
    if status is None:
        return True
    if status.BatteryFlag & NO_SYSTEM_BATTERY:
        return True
    return status.ACLineStatus == 1


def total_ram_gb(default=8.0):
    """Physical RAM in GiB (``default`` when the query is unavailable)."""
    mem = winapi.global_memory_status_ex()
    if mem is None:
        return float(default)
    try:
        return mem.ullTotalPhys / float(1 << 30)
    except Exception:
        return float(default)


def is_admin():
    """True when the process token is elevated (local administrator)."""
    return winapi.is_admin()
