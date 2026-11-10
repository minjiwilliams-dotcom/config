"""Windows platform layer for the CloudSync agent.

All ctypes declarations live in :mod:`cloudsync.platform.winapi`; the other
modules expose task-shaped helpers:

* :mod:`jobs`    - job-object CPU cap with kill-on-close (graceful: ``ok=False``)
* :mod:`power`   - SetThreadExecutionState + powercfg scheme helpers
* :mod:`tasks`   - schtasks create/query/run/delete wrappers
* :mod:`mutex`   - machine-wide single-instance lock
* :mod:`gpu`     - gpudetect.py invocation + gpu_profile.json loading

Importing this package has no side effects and works on non-Windows hosts (all
queries degrade to their documented "unavailable" values).
"""

from __future__ import annotations

from . import gpu, jobs, mutex, power, tasks, winapi
from .gpu import (
    PROFILE_NAME, ensure_gpu_profile, ensure_profile,
    ensure_profile_and_load, is_available, load_profile, read_profile,
    run_probe,
)
from .jobs import JobObject, create_cpu_cap_job
from .mutex import (
    DEFAULT_APP, Mutex, SingleInstance, acquire_single_instance,
    other_controller_pids,
)
from .power import (
    active_scheme, allow_sleep, keep_system_awake, set_active_scheme,
)
from .tasks import (
    DEFAULT_TASKS, DEFAULT_TASK_ROOT, HEALTH_TASK, MAIN_TASK,
    create, create_minute, create_onlogon, create_once, delete, delete_all,
    exists, query, quote_command, run, schtasks,
)

__all__ = [
    # submodules
    "winapi", "jobs", "power", "tasks", "mutex", "gpu",
    # jobs
    "JobObject", "create_cpu_cap_job",
    # power
    "keep_system_awake", "allow_sleep", "active_scheme", "set_active_scheme",
    # tasks
    "DEFAULT_TASK_ROOT", "MAIN_TASK", "HEALTH_TASK", "DEFAULT_TASKS",
    "quote_command", "schtasks", "create", "create_onlogon", "create_minute",
    "create_once", "query", "exists", "run", "end", "delete", "delete_all",
    # mutex
    "DEFAULT_APP", "Mutex", "SingleInstance", "acquire_single_instance",
    "other_controller_pids",
    # gpu
    "PROFILE_NAME", "is_available", "load_profile", "read_profile",
    "run_probe", "ensure_profile", "ensure_gpu_profile",
    "ensure_profile_and_load",
]
