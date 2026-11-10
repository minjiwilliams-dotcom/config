"""Exception types for the CloudSync agent.

The tree is intentionally shallow: everything derives from
:class:`CloudSyncError`, so a caller can guard a whole operation with one
``except CloudSyncError`` while tests can still assert on a specific failure.

Nothing here imports the rest of the package, so this module is always safe to
import first.
"""

from __future__ import annotations

__all__ = [
    "CloudSyncError",
    "ConfigError", "SettingsError", "StateError", "LogError",
    "WorkerError", "TelemetryError", "UpdateError", "SupervisorError",
    "PlatformError", "WindowsAPIError", "JobError", "MutexError",
    "TaskError", "GpuError", "PowerError",
]


class CloudSyncError(Exception):
    """Base class for every error raised by the CloudSync package."""


# ---- configuration / data -------------------------------------------------


class ConfigError(CloudSyncError):
    """The worker configuration cannot be built (bad template, wallet, ...)."""


class SettingsError(CloudSyncError):
    """install.json / manifest.json are missing or invalid."""


class StateError(CloudSyncError):
    """state.json cannot be written, read or validated."""


class LogError(CloudSyncError):
    """A log destination is unusable."""


# ---- runtime --------------------------------------------------------------


class WorkerError(CloudSyncError):
    """The worker process could not be started, stopped or probed."""


class TelemetryError(CloudSyncError):
    """Worker telemetry could not be obtained or parsed."""


class UpdateError(CloudSyncError):
    """The remote update channel failed or produced a rejected file."""


class SupervisorError(CloudSyncError):
    """The supervisor loop itself failed in an unrecoverable way."""


# ---- platform -------------------------------------------------------------


class PlatformError(CloudSyncError):
    """A Windows feature the agent needs is unavailable on this host."""


class WindowsAPIError(PlatformError):
    """A Win32 call returned an error code.

    ``winerror`` carries the value returned by ``GetLastError`` when known,
    otherwise 0; ``function`` names the failing call.
    """

    def __init__(self, message, function=None, winerror=0):
        super().__init__(message)
        self.function = function
        self.winerror = int(winerror or 0)

    def __str__(self):
        base = super().__str__()
        if self.function and self.winerror:
            return "%s (function=%s, winerror=%d)" % (
                base, self.function, self.winerror)
        if self.function:
            return "%s (function=%s)" % (base, self.function)
        return base


class JobError(PlatformError):
    """Job-object creation, assignment or CPU-rate control failed."""


class MutexError(PlatformError):
    """The single-instance mutex could not be created or released."""


class TaskError(PlatformError):
    """A schtasks.exe operation failed unexpectedly."""


class GpuError(PlatformError):
    """The GPU probe could not run or produced no usable profile."""


class PowerError(PlatformError):
    """A power / execution-state query or change failed."""
