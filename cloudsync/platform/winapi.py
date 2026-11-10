"""Windows API declarations (ctypes) for the CloudSync platform layer.

This is the ONLY module in the package that declares ctypes structures and
kernel32 / user32 / shell32 function prototypes.  Everything else calls the
thin, exception-free wrappers below, which keeps the rest of the package
importable (and unit-testable) on any operating system.

Design rules
------------
* No system access at import time: ``ctypes.WinDLL`` is called on first use,
  never at module load.  Importing this module has no side effects.
* Every wrapper is best effort.  On a non-Windows host, on a missing export or
  on a denied call it returns the documented "unavailable" sentinel (``None``,
  ``False`` or ``0``) instead of raising, so callers degrade gracefully.
* Handles are plain Python ints (0 means NULL).  They are owned - and closed -
  by the module that created them (``jobs``, ``mutex``).
* Mirrors the constants and structures used by the current single-file
  ``controller.py`` so behaviour (job CPU cap, mutex, idle gate, power status)
  stays identical.
"""

from __future__ import annotations

import ctypes
import sys

__all__ = [
    # constants
    "CREATE_NO_WINDOW", "DETACHED_PROCESS",
    "PROCESS_TERMINATE", "PROCESS_SET_QUOTA",
    "ERROR_ACCESS_DENIED", "ERROR_ALREADY_EXISTS",
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION",
    "JOB_OBJECT_EXTENDED_LIMIT_INFORMATION",
    "JOB_RATE_ENABLE", "JOB_RATE_HARD_CAP",
    "ES_CONTINUOUS", "ES_SYSTEM_REQUIRED", "ES_DISPLAY_REQUIRED",
    # structures
    "LASTINPUTINFO", "POWERSTATUS", "MEMORYSTATUSEX",
    "JOBOBJECT_BASIC_LIMIT_INFORMATION", "IO_COUNTERS",
    "JOBOBJECT_EXTENDED_LIMIT_INFORMATION",
    "JOBOBJECT_CPU_RATE_CONTROL_INFORMATION",
    # probes
    "available", "get_last_error", "creation_flags",
    "get_tick_count", "get_last_input_info",
    "get_system_power_status", "global_memory_status_ex",
    "set_thread_execution_state", "is_admin",
    # job objects
    "create_job_object", "set_information_job_object", "open_process",
    "assign_process_to_job_object", "close_handle",
    # mutex
    "create_mutex",
]

# ---------------------------------------------------------------------------
# constants (values match the Win32 SDK headers / controller.py)
# ---------------------------------------------------------------------------

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15   # JobObjectCpuRateControlInformation
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9      # JobObjectExtendedLimitInformation
JOB_RATE_ENABLE = 0x1
JOB_RATE_HARD_CAP = 0x4

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# ---------------------------------------------------------------------------
# structures
# ---------------------------------------------------------------------------


class LASTINPUTINFO(ctypes.Structure):
    """user32.GetLastInputInfo payload (tick of the last input event)."""

    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint32),
    ]


class POWERSTATUS(ctypes.Structure):
    """kernel32.GetSystemPowerStatus payload (SYSTEM_POWER_STATUS)."""

    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("Reserved1", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_uint32),
        ("BatteryFullLifeTime", ctypes.c_uint32),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    """kernel32.GlobalMemoryStatusEx payload."""

    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _CPU_RATE_UNION(ctypes.Union):
    _fields_ = [
        ("CpuRate", ctypes.c_uint32),
        ("Weight", ctypes.c_uint32),
        ("MinRate", ctypes.c_uint32),
    ]


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ControlFlags", ctypes.c_uint32),
        ("Rate", _CPU_RATE_UNION),
    ]


# ---------------------------------------------------------------------------
# lazy library loading (no side effects at import)
# ---------------------------------------------------------------------------

_LOADED = False
_K32 = None
_U32 = None
_SHELL32 = None


def _proto(fn, restype, argtypes):
    try:
        fn.restype = restype
        fn.argtypes = argtypes
    except Exception:
        pass
    return fn


def _libs():
    """Load and prototype the Win32 libraries once; return (k32, u32, shell32)."""
    global _LOADED, _K32, _U32, _SHELL32
    if _LOADED:
        return _K32, _U32, _SHELL32
    _LOADED = True
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None or sys.platform != "win32":
        return _K32, _U32, _SHELL32
    try:
        k32 = windll("kernel32", use_last_error=False)
        u32 = windll("user32", use_last_error=False)
        shell = windll("shell32", use_last_error=False)

        _proto(k32.GetTickCount, ctypes.c_uint32, [])
        _proto(k32.GetLastError, ctypes.c_uint32, [])
        _proto(k32.GetSystemPowerStatus, ctypes.c_int,
               [ctypes.POINTER(POWERSTATUS)])
        _proto(k32.GlobalMemoryStatusEx, ctypes.c_int,
               [ctypes.POINTER(MEMORYSTATUSEX)])
        _proto(k32.SetThreadExecutionState, ctypes.c_size_t,
               [ctypes.c_uint])
        _proto(k32.CreateJobObjectW, ctypes.c_void_p,
               [ctypes.c_void_p, ctypes.c_wchar_p])
        _proto(k32.SetInformationJobObject, ctypes.c_int,
               [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                ctypes.c_uint32])
        _proto(k32.OpenProcess, ctypes.c_void_p,
               [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32])
        _proto(k32.AssignProcessToJobObject, ctypes.c_int,
               [ctypes.c_void_p, ctypes.c_void_p])
        _proto(k32.CloseHandle, ctypes.c_int, [ctypes.c_void_p])
        _proto(k32.CreateMutexW, ctypes.c_void_p,
               [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p])
        _proto(u32.GetLastInputInfo, ctypes.c_int,
               [ctypes.POINTER(LASTINPUTINFO)])
        _proto(shell.IsUserAnAdmin, ctypes.c_int, [])

        _K32, _U32, _SHELL32 = k32, u32, shell
    except Exception:
        _K32 = _U32 = _SHELL32 = None
    return _K32, _U32, _SHELL32


def available():
    """True when the Win32 libraries could be loaded (Windows hosts only)."""
    k32, _u32, _shell = _libs()
    return k32 is not None


def creation_flags():
    """``CREATE_NO_WINDOW`` on Windows, 0 elsewhere (safe for ``subprocess``).

    POSIX ``subprocess`` rejects a non-zero ``creationflags``, so callers must
    use this helper rather than the raw constant when they may be exercised by
    the test suite on a non-Windows host.
    """
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def get_last_error():
    """Last Win32 error code of the calling thread; 0 when unavailable."""
    k32, _u32, _shell = _libs()
    if k32 is None:
        return 0
    try:
        return int(k32.GetLastError())
    except Exception:
        return 0


def get_tick_count():
    """32-bit GetTickCount value (wraps every ~49.7 days, like the original)."""
    k32, _u32, _shell = _libs()
    if k32 is None:
        return 0
    try:
        return int(k32.GetTickCount()) & 0xFFFFFFFF
    except Exception:
        return 0


def get_last_input_info():
    """Return a filled LASTINPUTINFO, or None when the call fails."""
    _k32, u32, _shell = _libs()
    if u32 is None:
        return None
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    try:
        if not u32.GetLastInputInfo(ctypes.byref(info)):
            return None
    except Exception:
        return None
    return info


def get_system_power_status():
    """Return a filled POWERSTATUS, or None when the call fails."""
    k32, _u32, _shell = _libs()
    if k32 is None:
        return None
    status = POWERSTATUS()
    try:
        if not k32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
    except Exception:
        return None
    return status


def global_memory_status_ex():
    """Return a filled MEMORYSTATUSEX, or None when the call fails."""
    k32, _u32, _shell = _libs()
    if k32 is None:
        return None
    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        if not k32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return None
    except Exception:
        return None
    return mem


def set_thread_execution_state(flags):
    """SetThreadExecutionState(flags); False when the call fails/unavailable."""
    k32, _u32, _shell = _libs()
    if k32 is None:
        return False
    try:
        return bool(k32.SetThreadExecutionState(ctypes.c_uint(flags)))
    except Exception:
        return False


def is_admin():
    """True when the current process token is a local administrator."""
    _k32, _u32, shell = _libs()
    if shell is None:
        return False
    try:
        return bool(shell.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# job objects
# ---------------------------------------------------------------------------


def create_job_object(name=None):
    """Create a job object; returns a handle int or 0 on failure."""
    k32, _u32, _shell = _libs()
    if k32 is None:
        return 0
    try:
        handle = k32.CreateJobObjectW(None, name)
    except Exception:
        return 0
    return int(handle) if handle else 0


def set_information_job_object(handle, info_class, info):
    """SetInformationJobObject(handle, class, info); False on failure."""
    k32, _u32, _shell = _libs()
    if k32 is None or not handle:
        return False
    try:
        return bool(k32.SetInformationJobObject(
            ctypes.c_void_p(int(handle)), int(info_class),
            ctypes.byref(info), ctypes.sizeof(info)))
    except Exception:
        return False


def open_process(pid, access=PROCESS_SET_QUOTA | PROCESS_TERMINATE):
    """OpenProcess(access, pid); returns a handle int or 0 on failure."""
    k32, _u32, _shell = _libs()
    if k32 is None:
        return 0
    try:
        handle = k32.OpenProcess(int(access), False, int(pid))
    except Exception:
        return 0
    return int(handle) if handle else 0


def assign_process_to_job_object(job_handle, process_handle):
    """AssignProcessToJobObject; False on failure."""
    k32, _u32, _shell = _libs()
    if k32 is None or not job_handle or not process_handle:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(
            ctypes.c_void_p(int(job_handle)),
            ctypes.c_void_p(int(process_handle))))
    except Exception:
        return False


def close_handle(handle):
    """CloseHandle; False on failure/unavailable."""
    k32, _u32, _shell = _libs()
    if k32 is None or not handle:
        return False
    try:
        return bool(k32.CloseHandle(ctypes.c_void_p(int(handle))))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# mutex
# ---------------------------------------------------------------------------


def create_mutex(name, initial_owner=False):
    """CreateMutexW; returns (handle, last_error). Handle 0 means failure.

    ``last_error`` is checked by callers for ERROR_ALREADY_EXISTS (183) and
    ERROR_ACCESS_DENIED (5), exactly like controller.py does.
    """
    k32, _u32, _shell = _libs()
    if k32 is None:
        return 0, 0
    try:
        handle = k32.CreateMutexW(None, 1 if initial_owner else 0, name)
        err = int(k32.GetLastError())
    except Exception:
        return 0, 0
    return (int(handle) if handle else 0), err
