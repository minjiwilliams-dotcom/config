"""Diagnostics: "what broke, and who owns it".

Two questions, two small answers:

* :func:`classify` maps a failure to exactly one owner module - an exception
  from :mod:`cloudsync.core.errors`, an exception class, or a health/status
  string (state keys, watchdog check names, log keywords).
* :func:`snapshot` / :func:`report_lines` render what is healthy right now, per
  module, using only the evidence the caller already has (Settings object,
  ``state.json`` payload, task probe result, health-check results). No probes are
  launched from here, so a diagnostic report cannot itself disturb the miner.

Ownership tables are data (:data:`MODULES`, :data:`EXCEPTION_OWNERS`,
:data:`STATUS_OWNERS`) so a new module only has to be added in one place.
Nothing runs at import time; all functions are pure with respect to their
arguments (``snapshot`` only reads the objects it is given).
"""

from __future__ import annotations

import os

from ..config.overrides import OVERRIDES_FILE
from .errors import (
    CloudSyncError,
    ConfigError,
    GpuError,
    JobError,
    LogError,
    MutexError,
    PlatformError,
    PowerError,
    SettingsError,
    StateError,
    SupervisorError,
    TaskError,
    TelemetryError,
    UpdateError,
    WindowsAPIError,
    WorkerError,
)

__all__ = [
    "MODULES",
    "UNKNOWN",
    "EXCEPTION_OWNERS",
    "STATUS_OWNERS",
    "classify",
    "snapshot",
    "report_lines",
]

#: Sentinel returned by :func:`classify` when no module owns the failure.
UNKNOWN = "unknown"

#: Module path -> one-line responsibility (also the report order is sorted()).
MODULES = {
    "cloudsync": "package root: entry point and version",
    "cloudsync.__main__": "python -m cloudsync entry point (--version, supervisor.main)",
    "cloudsync.config.settings": "install.json + manifest.json settings and paths",
    "cloudsync.config.profiles": "full/trickle worker profiles and GPU profile loading",
    "cloudsync.config.overrides": "per-host whitelisted override merge",
    "cloudsync.core.errors": "exception hierarchy",
    "cloudsync.core.logging": "rotating controller/worker log files",
    "cloudsync.core.state": "atomic state.json heartbeat writer",
    "cloudsync.core.clock": "monotonic clock, idle seconds, power status",
    "cloudsync.core.diagnostics": "failure ownership and per-module health snapshot",
    "cloudsync.platform.winapi": "ctypes kernel32/user32 declarations and helpers",
    "cloudsync.platform.jobs": "job-object CPU cap (kill-on-close)",
    "cloudsync.platform.power": "SetThreadExecutionState and power schemes",
    "cloudsync.platform.tasks": "schtasks.exe create/query/delete wrappers",
    "cloudsync.platform.mutex": "machine-wide single-instance mutex",
    "cloudsync.platform.gpu": "gpudetect.py invocation and gpu_profile.json",
    "cloudsync.miner.config": "build config.dat from profile + settings",
    "cloudsync.miner.worker": "spawn/stop/reap the worker process",
    "cloudsync.miner.telemetry": "parse the worker log (hashrate/shares)",
    "cloudsync.supervisor": "reconcile loop and circuit breakers",
    "cloudsync.updater": "manifest-pinned update and .prev rollback",
    "cloudsync.health": "pure watchdog checks",
    "cloudsync.lifecycle": "install/teardown: tasks, Run key, Defender, files",
}

#: Exception class -> owning module. Subclasses are resolved through their MRO,
#: so the most specific entry wins (e.g. JobError before PlatformError).
EXCEPTION_OWNERS = {
    SettingsError: "cloudsync.config.settings",
    ConfigError: "cloudsync.miner.config",
    StateError: "cloudsync.core.state",
    LogError: "cloudsync.core.logging",
    WorkerError: "cloudsync.miner.worker",
    TelemetryError: "cloudsync.miner.telemetry",
    UpdateError: "cloudsync.updater",
    SupervisorError: "cloudsync.supervisor",
    TaskError: "cloudsync.platform.tasks",
    MutexError: "cloudsync.platform.mutex",
    JobError: "cloudsync.platform.jobs",
    GpuError: "cloudsync.platform.gpu",
    PowerError: "cloudsync.platform.power",
    WindowsAPIError: "cloudsync.platform.winapi",
    PlatformError: "cloudsync.platform.winapi",
    CloudSyncError: "cloudsync",
}

#: Status/health keyword -> owning module. Exact keys win over substring matches;
#: substring matches are tried longest-first so ``tasks_missing`` beats ``task``.
STATUS_OWNERS = {
    # lifecycle: install / persistence problems
    "missing_files": "cloudsync.lifecycle",
    "tampered": "cloudsync.lifecycle",
    "tasks_present": "cloudsync.lifecycle",
    "tasks_missing": "cloudsync.lifecycle",
    "task_missing": "cloudsync.lifecycle",
    "run_key": "cloudsync.lifecycle",
    "runkey": "cloudsync.lifecycle",
    "autostart": "cloudsync.lifecycle",
    "defender": "cloudsync.lifecycle",
    "exclusion": "cloudsync.lifecycle",
    "install.json": "cloudsync.lifecycle",
    "adoption": "cloudsync.lifecycle",
    "destroy_requested": "cloudsync.lifecycle",
    # state / supervisor heartbeat
    "stale_state": "cloudsync.supervisor",
    "state.json": "cloudsync.core.state",
    "state": "cloudsync.core.state",
    # configuration
    "settings": "cloudsync.config.settings",
    "manifest": "cloudsync.updater",
    "update": "cloudsync.updater",
    "probation": "cloudsync.updater",
    "rollback": "cloudsync.updater",
    "overrides": "cloudsync.config.overrides",
    "profile": "cloudsync.config.profiles",
    "wallet": "cloudsync.miner.config",
    "config.dat": "cloudsync.miner.config",
    "config": "cloudsync.miner.config",
    # runtime
    "controller_running": "cloudsync.supervisor",
    "worker": "cloudsync.miner.worker",
    "crash": "cloudsync.miner.worker",
    "backoff": "cloudsync.miner.worker",
    "spawn": "cloudsync.miner.worker",
    "telemetry": "cloudsync.miner.telemetry",
    "hashrate": "cloudsync.miner.telemetry",
    "shares": "cloudsync.miner.telemetry",
    "job": "cloudsync.platform.jobs",
    "cpu_cap": "cloudsync.platform.jobs",
    "mutex": "cloudsync.platform.mutex",
    "instance": "cloudsync.platform.mutex",
    "task": "cloudsync.platform.tasks",
    "schtasks": "cloudsync.platform.tasks",
    "gpu": "cloudsync.platform.gpu",
    "opencl": "cloudsync.platform.gpu",
    "power": "cloudsync.platform.power",
    "battery": "cloudsync.platform.power",
    "idle": "cloudsync.core.clock",
    "clock": "cloudsync.core.clock",
    "winapi": "cloudsync.platform.winapi",
    "windows": "cloudsync.platform.winapi",
    "logging": "cloudsync.core.logging",
    "log": "cloudsync.core.logging",
    "health": "cloudsync.health",
    "watchdog": "cloudsync.health",
    "supervisor": "cloudsync.supervisor",
    "loop": "cloudsync.supervisor",
    "lifecycle": "cloudsync.lifecycle",
}

_STATUS_TOKENS = tuple(sorted(STATUS_OWNERS, key=len, reverse=True))

#: Exception class names as lower-case keywords, so a formatted error string such
#: as ``"SettingsError: install.json missing"`` routes by its class, not by the
#: words in the message. Longest first keeps ``WindowsAPIError`` ahead of ``Error``.
_EXCEPTION_TOKENS = tuple(
    sorted(
        ((cls.__name__.lower(), owner) for cls, owner in EXCEPTION_OWNERS.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


def _owner_for_exception(exc_type):
    """Owning module for an exception class, walking its MRO; UNKNOWN if unowned."""
    for candidate in getattr(exc_type, "__mro__", ()):
        owner = EXCEPTION_OWNERS.get(candidate)
        if owner:
            return owner
    return UNKNOWN


def classify(exc_or_status):
    """Return the module path that owns *exc_or_status*.

    Accepts an exception instance, an exception class, or a status/health string
    (e.g. ``"stale_state"``, ``"tasks_missing"``, ``"worker crashed"``). Returns
    one of the :data:`MODULES` keys, or :data:`UNKNOWN` when nothing matches.
    """
    if exc_or_status is None:
        return UNKNOWN

    if isinstance(exc_or_status, BaseException):
        return _owner_for_exception(type(exc_or_status))
    if isinstance(exc_or_status, type) and issubclass(exc_or_status, BaseException):
        return _owner_for_exception(exc_or_status)

    text = str(exc_or_status).strip().lower()
    if not text:
        return UNKNOWN

    owner = STATUS_OWNERS.get(text)
    if owner is not None:
        return owner
    for token, token_owner in _EXCEPTION_TOKENS:
        if token in text:
            return token_owner
    for token in _STATUS_TOKENS:
        if token in text:
            return STATUS_OWNERS[token]
    return UNKNOWN


def _set(status, module, ok):
    """Record a module's state ("ok" when *ok* is truthy, else "unknown")."""
    if module in status:
        status[module] = "ok" if ok else "unknown"


def snapshot(settings=None, state=None, tasks_ok=None, health=None):
    """Return ``{module: "ok" | "unknown"}`` from the evidence the caller has.

    All four arguments are optional; a module is ``"ok"`` only when an input
    positively confirms it, otherwise ``"unknown"``. No probes, subprocesses or
    file writes are performed here.

    * ``settings``  -- a :class:`cloudsync.config.settings.Settings` instance.
    * ``state``     -- the parsed ``state.json`` mapping.
    * ``tasks_ok``  -- result of the scheduled-task presence probe (True/False).
    * ``health``    -- ``{status: healthy_bool}`` where True means healthy; each
      status is routed to its owner with :func:`classify`.
    """
    status = {module: "unknown" for module in MODULES}

    if settings is not None:
        install_dir = getattr(settings, "install_dir", "") or ""
        _set(status, "cloudsync.config.settings", bool(install_dir))
        # Profiles are static data: available as soon as the package imports.
        _set(status, "cloudsync.config.profiles", True)
        _set(status, "cloudsync.config.overrides",
             os.path.isfile(settings.path(OVERRIDES_FILE)))
        _set(status, "cloudsync.core.logging", bool(install_dir))
        _set(status, "cloudsync.updater", bool(getattr(settings, "remote_update", False)))
        _set(status, "cloudsync.lifecycle", os.path.isfile(settings.path("install.json")))
        _set(status, "cloudsync.platform.gpu",
             os.path.isfile(settings.path("gpu_profile.json")))

    state_map = state if isinstance(state, dict) else None
    if state_map is not None:
        _set(status, "cloudsync.core.state", True)
        _set(status, "cloudsync.supervisor", bool(state_map.get("pid")))
        _set(status, "cloudsync.core.clock", "idle_s" in state_map)
        _set(status, "cloudsync.miner.config", bool(state_map.get("profile")))
        _set(status, "cloudsync.miner.worker", state_map.get("running") is not None)
        _set(status, "cloudsync.miner.telemetry",
             state_map.get("hashrate") not in (None, "", "n/a"))
        _set(status, "cloudsync.platform.jobs", "job" in state_map)
        _set(status, "cloudsync.platform.power", "wanted" in state_map)
        _set(status, "cloudsync.platform.mutex", bool(state_map.get("pid")))
        _set(status, "cloudsync.platform.gpu",
             bool(state_map.get("gpu")) or bool(state_map.get("gpu_ready")))

    if tasks_ok is not None:
        # A completed probe proves the task layer; presence belongs to lifecycle.
        _set(status, "cloudsync.platform.tasks", True)
        _set(status, "cloudsync.lifecycle", bool(tasks_ok))

    if isinstance(health, dict):
        _set(status, "cloudsync.health", True)
        for key, healthy in health.items():
            owner = classify(key)
            if owner != UNKNOWN:
                _set(status, owner, bool(healthy))

    # This report is only produced by a running, importable package.
    _set(status, "cloudsync.core.diagnostics", True)
    return status


def _short_name(module):
    """Drop the ``cloudsync.`` prefix so report lines stay narrow."""
    prefix = "cloudsync."
    return module[len(prefix):] if module.startswith(prefix) else module


def report_lines(snapshot):
    """Render a :func:`snapshot` result as ``"<module> ........ OK"`` lines.

    Lines are sorted by module path so two reports are diffable. ``"unknown"``
    is printed as ``UNKNOWN`` and means "not confirmed healthy" - use
    :func:`classify` on the actual error/status for the owning module.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return []
    labels = {module: _short_name(module) for module in snapshot}
    width = max(len(label) for label in labels.values()) + 2
    lines = []
    for module in sorted(snapshot):
        label = labels[module]
        dots = "." * max(2, width - len(label))
        lines.append("%s %s %s" % (label, dots, str(snapshot[module]).upper()))
    return lines
