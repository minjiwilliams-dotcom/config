"""``state.json`` - the heartbeat the watchdog and the RAT read.

The payload written here has the same 18 keys, in the same order, as the
current single-file ``controller.py``.  Nothing is added or renamed - the
PowerShell watchdog, the Pulsar plugin and the operator all read this file.

``write()`` is atomic: bytes are written to ``<path>.tmp``, flushed, fsynced and
then ``os.replace``d over the target, so a reader never sees a half-written
JSON document.  Encoding is UTF-8 *without* a BOM (``json.dumps`` output is
ASCII-safe by default), matching what the controller produced.

``build_payload()`` accepts a "worker view": either the live ``Worker`` object
(or anything duck-typed like it) or a plain mapping.  This keeps the schema in
one place while ``miner.worker`` / ``supervisor`` stay free of formatting code.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping

from ..platform import winapi
from . import clock

__all__ = [
    "STATE_NAME", "STATE_KEYS",
    "atomic_write", "write", "read", "build_payload", "write_state",
]

STATE_NAME = "state.json"

# Exact key set (and order) of the current state.json document.
STATE_KEYS = (
    "ts", "time", "pid", "running", "wanted", "profile", "throttled",
    "cpu_cap_pct", "job", "idle_s", "uptime_s", "gpu", "gpu_ready",
    "hashrate", "accepted", "rejected", "admin", "ram_gb",
)


# ---------------------------------------------------------------------------
# atomic file write / JSON read
# ---------------------------------------------------------------------------


def atomic_write(path, data):
    """Atomically replace ``path`` with ``data`` (str, bytes or JSON value).

    Mirrors ``controller.atomic_write``: temp file in the same directory,
    flush + fsync, then ``os.replace``.  Exceptions (OSError) propagate so the
    caller can decide whether a failed heartbeat is fatal.
    """
    if isinstance(data, str):
        payload = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        payload = bytes(data)
    else:
        payload = json.dumps(data, indent=2).encode("utf-8")

    target = os.fspath(path)
    tmp = target + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def write(path, payload):
    """Write the state document atomically.

    ``payload`` is normally the dict returned by :func:`build_payload`; str and
    bytes are written verbatim so the helper is reusable for other JSON files.
    """
    if isinstance(payload, (str, bytes, bytearray, memoryview)):
        atomic_write(path, payload)
    else:
        atomic_write(path, json.dumps(payload, indent=2))
    return True


def read(path, default=None):
    """Read a JSON document; tolerate a UTF-8 BOM (PowerShell 5.1 files).

    Returns ``default`` when the file is missing or malformed, like the old
    ``read_json`` helper.
    """
    try:
        with open(os.fspath(path), "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def _get(view, name, default=None):
    """Read an attribute from an object or a key from a mapping."""
    if view is None:
        return default
    if isinstance(view, Mapping):
        return view.get(name, default)
    return getattr(view, name, default)


def _call(view, name, default=None):
    """Call ``view.name()`` when callable, else return the attribute value."""
    value = _get(view, name, default)
    if callable(value):
        try:
            return value()
        except Exception:
            return default
    return value


def _flag(view, name):
    return bool(_get(view, name, False))


def _telemetry(view):
    tele = _get(view, "tele", None)
    if isinstance(tele, Mapping):
        return tele
    return {}


def build_payload(settings, worker_view, wanted, idle, cpu_cap_pct=100):
    """Build the state.json document.

    Parameters
    ----------
    settings : object or None
        Accepted for API symmetry with the rest of the agent; the payload does
        not currently derive anything from it (the unchanged schema does not
        carry install settings).
    worker_view : object or mapping
        Worker-like source.  Recognised members: ``alive()``/``running``,
        ``profile``, ``throttled``, ``job_ok``, ``uptime()``/``uptime_s``,
        ``gpu_applied``, ``tele`` (mapping with ``gpu_ready``, ``accepted``,
        ``rejected``) and ``hashrate_text()``/``hashrate``.
    wanted : bool
        Desired worker state for this tick.
    idle : float
        Seconds since the last user input (see :func:`core.clock.idle_seconds`).
    cpu_cap_pct : int
        Current job-object CPU cap in percent (100 = uncapped).
    """
    _ = settings  # kept in the signature for parity with other builders
    tele = _telemetry(worker_view)

    running = _call(worker_view, "alive", None)
    if running is None:
        running = _flag(worker_view, "running")
    running = bool(running)

    profile = _get(worker_view, "profile", "full") or "full"

    uptime = _call(worker_view, "uptime", None)
    if uptime is None:
        uptime = _get(worker_view, "uptime_s", 0.0) or 0.0

    hashrate = _call(worker_view, "hashrate_text", None)
    if hashrate is None:
        hashrate = _get(worker_view, "hashrate", "n/a") or "n/a"

    return {
        "ts": time.time(),
        "time": clock.timestamp(),
        "pid": os.getpid(),
        "running": running,
        "wanted": bool(wanted),
        "profile": profile,
        "throttled": _flag(worker_view, "throttled"),
        "cpu_cap_pct": int(cpu_cap_pct),
        "job": _flag(worker_view, "job_ok"),
        "idle_s": round(float(idle), 1),
        "uptime_s": round(float(uptime), 1),
        "gpu": _flag(worker_view, "gpu_applied"),
        "gpu_ready": bool(tele.get("gpu_ready", False)),
        "hashrate": str(hashrate),
        "accepted": tele.get("accepted"),
        "rejected": tele.get("rejected"),
        "admin": winapi.is_admin(),
        "ram_gb": round(clock.total_ram_gb(), 1),
    }


def write_state(path, settings, worker_view, wanted, idle, cpu_cap_pct=100):
    """Build and atomically write state.json; returns the payload that was used."""
    payload = build_payload(settings, worker_view, wanted, idle, cpu_cap_pct)
    write(path, payload)
    return payload
