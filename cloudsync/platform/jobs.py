"""Job-object CPU cap: the "max-threads-on-active" behaviour without a rebuild.

The current controller puts the worker into a Windows job object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and then caps the job's total CPU with
``JobObjectCpuRateControlInformation`` while the user is at the keyboard.  This
module exposes the same mechanism as an object:

    job = create_cpu_cap_job(100)      # job.ok is False if unavailable
    job.assign(worker_pid)             # worker now dies with this process
    job.set_cpu_rate(25)               # hard-cap CPU at 25 %
    job.set_cpu_rate(100)              # release the cap

Any failure (non-Windows host, restricted token, policy-blocked job objects)
degrades to ``ok == False`` and ``assign()``/``set_cpu_rate()`` returning
False; the caller then runs without throttling, exactly like the old code's
``job_ok = False`` path.  Nothing is created at import time.
"""

from __future__ import annotations

from ..platform import winapi

__all__ = [
    "JobObject", "create_cpu_cap_job",
    "JOB_CPU_CLASS", "JOB_EXT_CLASS", "JOB_RATE_ENABLE", "JOB_RATE_HARD_CAP",
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "MIN_PERCENT", "MAX_PERCENT",
]

JOB_CPU_CLASS = winapi.JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION
JOB_EXT_CLASS = winapi.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
JOB_RATE_ENABLE = winapi.JOB_RATE_ENABLE
JOB_RATE_HARD_CAP = winapi.JOB_RATE_HARD_CAP
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = winapi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

MIN_PERCENT = 1
MAX_PERCENT = 100


class JobObject:
    """A Windows job object with an optional CPU rate ceiling.

    ``handle`` is 0 when creation failed; ``ok`` reflects whether the job exists
    (and therefore whether assignment/capping can be attempted).  ``rate_ok``
    records the result of the last :meth:`set_cpu_rate` call.
    """

    def __init__(self, percent=None, name=None):
        self.handle = 0
        self.ok = False
        self.rate_ok = False
        self.kill_on_close = False
        self._closed = False
        self.handle = winapi.create_job_object(name)
        if not self.handle:
            return

        limits = winapi.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.kill_on_close = winapi.set_information_job_object(
            self.handle, JOB_EXT_CLASS, limits)
        self.ok = True
        self.rate_ok = False
        if percent is not None:
            self.rate_ok = self.set_cpu_rate(percent)

    # -- lifecycle ---------------------------------------------------------

    def __repr__(self):
        return "JobObject(handle=%d, ok=%s, rate_ok=%s)" % (
            self.handle, self.ok, self.rate_ok)

    def __bool__(self):
        return bool(self.ok)

    def close(self):
        """Close the handle.  Kills assigned processes (kill-on-close)."""
        if self.handle and not self._closed:
            winapi.close_handle(self.handle)
            self._closed = True
        self.handle = 0
        self.ok = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # -- worker control ----------------------------------------------------

    def assign(self, pid):
        """Put a process inside the job; False when any step is unavailable."""
        if not self.handle or not pid:
            return False
        process = winapi.open_process(
            int(pid), winapi.PROCESS_SET_QUOTA | winapi.PROCESS_TERMINATE)
        if not process:
            return False
        try:
            return winapi.assign_process_to_job_object(self.handle, process)
        finally:
            winapi.close_handle(process)

    def set_cpu_rate(self, percent):
        """Hard-cap the job's total CPU at ``percent`` (100 = uncapped)."""
        if not self.handle:
            return False
        info = winapi.JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        info.ControlFlags = JOB_RATE_ENABLE | JOB_RATE_HARD_CAP
        # JobObjectCpuRateControlInformation.CpuRate is in 1/100 of a percent.
        info.Rate.CpuRate = max(100, min(10000, int(percent * 100)))
        self.rate_ok = winapi.set_information_job_object(
            self.handle, JOB_CPU_CLASS, info)
        return self.rate_ok


def create_cpu_cap_job(percent=100, name=None):
    """Create a kill-on-close job object, optionally capped at ``percent``.

    Returns a :class:`JobObject`; check ``job.ok`` before relying on it.
    """
    return JobObject(percent=percent, name=name)
