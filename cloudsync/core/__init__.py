"""Core building blocks: errors, logging, state heartbeat and clock queries.

Importing this package has no side effects; submodules only touch the system
when their functions are called.  The frequently used names are re-exported for
convenience (``from cloudsync.core import write_state``), while the modules
remain importable individually (``from cloudsync.core.clock import idle_seconds``).
"""

from __future__ import annotations

from . import clock, errors, logging, state
from .clock import (
    elapsed, idle_seconds, is_admin, monotonic, now, on_ac_power, on_battery,
    power_status, timestamp, total_ram_gb,
)
from .errors import CloudSyncError
from .logging import (
    HEALTH_LOG, MAX_BYTES, ROTATE_SUFFIX, RotatingLog, WORKER_LOG,
    configure, get_default, log, logger_for, rotate, tail,
)
from .state import (
    STATE_KEYS, STATE_NAME, atomic_write, build_payload, read, write,
    write_state,
)

__all__ = [
    # submodules
    "clock", "errors", "logging", "state",
    # errors
    "CloudSyncError",
    # clock
    "now", "monotonic", "elapsed", "timestamp", "idle_seconds", "on_battery",
    "on_ac_power", "power_status", "total_ram_gb", "is_admin",
    # logging
    "MAX_BYTES", "ROTATE_SUFFIX", "WORKER_LOG", "HEALTH_LOG",
    "RotatingLog", "rotate", "logger_for", "configure", "get_default", "log",
    "tail",
    # state
    "STATE_NAME", "STATE_KEYS", "atomic_write", "write", "read",
    "build_payload", "write_state",
]
