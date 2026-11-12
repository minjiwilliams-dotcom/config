"""Worker process lifecycle: spawn, graceful stop, reap, job object, orphans.

Feature parity with the ``Worker`` class plus the process helpers in
``F:\\Test Miner\\controller.py``:

* the config is rebuilt and written atomically to ``config.dat`` before every
  start (a profile switch is a graceful restart, never a live reload);
* the worker is spawned console-less in the install directory and put into a
  kill-on-close job object so it can never outlive the supervisor;
* ``stop()`` kills gently first (``taskkill`` without ``/F``) after a short
  warm-up, so an OpenCL context is not torn down mid-creation;
* ``reap()`` counts fast crashes, drops the GPU block after two of them and arms
  an exponential backoff (60 s GPU / 20 s CPU, capped at 900 s);
* ``sweep_orphans()`` kills workers left behind by a previous supervisor, so two
  miners never silently halve each other's hashrate.

Every operating-system dependency is injectable (``process_factory``,
``terminate_fn``, ``job``/``job_factory``, ``clock``, ``sleep``,
``driver_reset_probe``) so the lifecycle can be tested with a fake child
process.  Importing this module does no work; nothing is spawned at import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import config as miner_config
from .telemetry import LogTailSource, apply_delta, hashrate_text
from ..config.profiles import get_profile, load_gpu_profile
from ..config.settings import DEFAULT_BINARY, resolve_binary

__all__ = [
    "CONFIG_NAME",
    "DEFAULT_BINARY",
    "FAST_FAIL_SECONDS",
    "WARMUP_SECONDS",
    "WORKER_LOG_NAME",
    "CREATE_NO_WINDOW",
    "Worker",
    "list_worker_pids",
    "sweep_orphans",
    "spawn_flags",
]

CONFIG_NAME = "config.dat"
WORKER_LOG_NAME = "CloudSync.log"
WORKER_LOG_MAX = 2 * 1024 * 1024

FAST_FAIL_SECONDS = 30.0     # died sooner than this = a "fast" crash
#: Grace period before a stop kills the worker (was 30 s of real wall time, which
#: made every supervisor restart take half a minute). 5 s is long enough for an
#: OpenCL context to settle, and a stop now returns within ~10 s.
WARMUP_SECONDS = 5.0
BACKOFF_GPU_BASE = 60.0      # first GPU fast-crash backoff
BACKOFF_CPU_BASE = 20.0
BACKOFF_MAX = 900.0

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def spawn_flags():
    """``CREATE_NO_WINDOW`` on Windows, 0 elsewhere (safe for subprocess)."""
    try:
        from ..platform.winapi import creation_flags
        return creation_flags()
    except Exception:
        return CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _noop_log(_message):
    return None


def _default_logger(install_dir):
    """Return the controller log writer for *install_dir* (best effort)."""
    try:
        from ..core.logging import CONTROLLER_LOG, logger_for
        return logger_for(install_dir, CONTROLLER_LOG).write
    except Exception:
        return _noop_log


def _install_dir(settings):
    if settings is None:
        return os.getcwd()
    if isinstance(settings, (str, bytes, os.PathLike)):
        return os.fspath(settings)
    return getattr(settings, "install_dir", None) or os.getcwd()


def _path(settings, name):
    if settings is not None and not isinstance(settings, (str, bytes, os.PathLike)):
        helper = getattr(settings, "path", None)
        if callable(helper):
            try:
                return helper(name)
            except Exception:
                pass
    return os.path.join(_install_dir(settings), name)


def _atomic_write(path, data):
    """Write *data* through a same-directory temp file + ``os.replace``."""
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _rotate(path, max_bytes=WORKER_LOG_MAX):
    """Rename *path* to ``path.1`` when it exceeds the cap; never raises."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return False
        previous = path + ".1"
        if os.path.exists(previous):
            os.remove(previous)
        os.replace(path, previous)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# default probes (lazy imports; injectable for tests)
# ---------------------------------------------------------------------------


def _default_config_builder(settings, profile, gpu_profile, *,
                            use_gpu=True, driver_reset_recently=False, log=None):
    return miner_config.build(
        settings, profile, gpu_profile,
        use_gpu=use_gpu,
        driver_reset_recently=driver_reset_recently,
        log=log,
    )


def _taskkill_graceful(pid):
    """Ask the worker to close (no ``/F``), so it can free its GPU context."""
    subprocess.run(
        ["taskkill.exe", "/PID", str(int(pid))],
        timeout=20,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=spawn_flags(),
    )


_DRIVER_RESET_PS = (
    "$n=0; try { $n=(Get-WinEvent -FilterHashtable @{LogName='System';"
    "Id=4101,13,14,153; StartTime=(Get-Date).AddMinutes(-%d)} -MaxEvents 5 "
    "-ErrorAction Stop | Measure-Object).Count } catch { $n=0 }; $n"
)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class Worker:
    """One mining worker process, supervised by :class:`cloudsync.supervisor.Supervisor`.

    Attributes mirror the controller's worker (the supervisor and the state
    payload read them): ``proc``, ``started_at``, ``fast_failures``,
    ``backoff_until``, ``use_gpu``, ``gpu_applied``, ``profile``, ``last_switch``,
    ``job_ok``, ``throttled``, ``tele``.
    """

    def __init__(self, settings, *,
                 config_builder=None,
                 gpu_profile_loader=None,
                 telemetry_source=None,
                 job=None,
                 job_factory=None,
                 process_factory=None,
                 log=None,
                 clock=None,
                 sleep=None,
                 terminate_fn=None,
                 driver_reset_probe=None,
                 warmup_seconds=WARMUP_SECONDS):
        self.settings = settings
        self.s = settings                     # controller.py naming

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

        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self._log = log or _default_logger(_install_dir(settings))
        self._config_builder = config_builder
        self._gpu_profile_loader = gpu_profile_loader or (
            lambda: load_gpu_profile(_path(self.settings, "gpu_profile.json")))
        self._telemetry_source = telemetry_source or LogTailSource(
            _path(settings, WORKER_LOG_NAME))
        self._job = job
        self._job_factory = job_factory
        self._job_lookup = job is not None
        self._process_factory = process_factory
        self._terminate = terminate_fn or _taskkill_graceful
        self._driver_reset_probe = driver_reset_probe
        self._driver_reset_cache = None
        self._warmup_seconds = float(warmup_seconds)

    # -- introspection ------------------------------------------------------

    def __repr__(self):
        return "Worker(pid=%s, profile=%r, alive=%s)" % (
            self.pid, self.profile, self.alive())

    @property
    def pid(self):
        """PID of the running worker, or 0 when it is not running."""
        proc = self.proc
        if proc is None:
            return 0
        try:
            return int(getattr(proc, "pid", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def alive(self):
        """True while the worker child process is running."""
        return self.proc is not None and self.proc.poll() is None

    def uptime(self):
        """Seconds the current worker has been running (0.0 when stopped)."""
        if not self.alive():
            return 0.0
        return max(0.0, self._clock() - self.started_at)

    # -- job object ---------------------------------------------------------

    def _ensure_job(self):
        """Create the job object once (lazily); ``None`` when unavailable."""
        if self._job_lookup:
            return self._job
        self._job_lookup = True
        try:
            if self._job_factory is not None:
                self._job = self._job_factory()
            else:
                from ..platform.jobs import create_cpu_cap_job
                self._job = create_cpu_cap_job(100)
        except Exception:
            self._job = None
        return self._job

    def can_cap(self):
        """True when a usable job object exists (the supervisor picks ``full``)."""
        job = self._ensure_job()
        return bool(job is not None and getattr(job, "ok", False))

    def set_cpu_rate(self, percent):
        """Cap the job's total CPU (100 = uncapped); False when unavailable."""
        job = self._ensure_job()
        if job is None:
            return False
        for name in ("set_cpu_rate", "set_percent"):
            setter = getattr(job, name, None)
            if callable(setter):
                try:
                    return bool(setter(int(percent)))
                except Exception:
                    return False
        return False

    def _assign_job(self, pid):
        job = self._ensure_job()
        assign = getattr(job, "assign", None) if job is not None else None
        if not callable(assign) or not pid:
            return False
        try:
            ok = bool(assign(int(pid)))
        except Exception:
            return False
        if ok:
            self.set_cpu_rate(100)
        return ok

    # -- config / spawn -----------------------------------------------------

    def _resolve_binary(self):
        """Return ``(name, path)`` for the worker binary (legacy-name tolerant)."""
        settings = self.settings
        install_dir = _install_dir(settings)
        preferred = getattr(settings, "binary", DEFAULT_BINARY) or DEFAULT_BINARY
        current = getattr(settings, "bin_path", None) or _path(settings, preferred)
        if os.path.isfile(current):
            return preferred, current
        name, path = resolve_binary(install_dir, preferred)
        if os.path.isfile(path):
            self._log("primary binary missing - falling back to %s" % name)
            for attribute, value in (("binary", name), ("bin_path", path)):
                try:
                    setattr(settings, attribute, value)
                except Exception:
                    pass
        return name, path

    def _load_gpu_profile(self):
        try:
            return self._gpu_profile_loader()
        except Exception:
            return None

    def _driver_recently_reset(self):
        """Cached TDR probe: a driver that reset recently loses its GPU block."""
        if self._driver_reset_cache is not None:
            return self._driver_reset_cache
        probe = self._driver_reset_probe
        if probe is None:
            result = self._probe_driver_reset()
        else:
            try:
                result = bool(probe())
            except Exception:
                result = False
        self._driver_reset_cache = bool(result)
        if self._driver_reset_cache:
            self._log("display driver reset detected in the last 30 min -> CPU only")
        return self._driver_reset_cache

    def _probe_driver_reset(self, minutes=30):
        """Query the System event log for recent display driver resets."""
        try:
            output = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 _DRIVER_RESET_PS % int(minutes)],
                timeout=45,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=spawn_flags(),
            ).stdout
            return int((output or b"0").decode("utf-8", "replace").strip() or 0) > 0
        except Exception:
            return False

    def _build_worker_config(self, profile_name, gpu_profile):
        builder = self._config_builder or _default_config_builder
        return builder(
            self.settings,
            get_profile(profile_name),
            gpu_profile,
            use_gpu=self.use_gpu,
            driver_reset_recently=self._driver_recently_reset(),
            log=self._log,
        )

    def _worker_args(self, binary_path):
        return [
            binary_path,
            "-c", os.path.basename(_path(self.settings, CONFIG_NAME)),
            "--log-file", os.path.basename(_path(self.settings, WORKER_LOG_NAME)),
            "--no-color", "--no-title",
        ]

    def _spawn(self, args):
        return subprocess.Popen(
            args,
            cwd=_install_dir(self.settings),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=spawn_flags(),
        )

    # -- lifecycle ----------------------------------------------------------

    def start(self, profile="full", *, gpu=None):
        """Build the config and spawn the worker; False when that failed."""
        if gpu is not None:
            self.use_gpu = bool(gpu)

        _name, binary = self._resolve_binary()
        if not binary or not os.path.isfile(binary):
            self._log("worker binary missing: %s" % binary)
            return False

        try:
            config = self._build_worker_config(profile, self._load_gpu_profile())
        except Exception as exc:
            self._log("config build failed: %r" % (exc,))
            self.backoff_until = self._clock() + 120
            return False

        try:
            _atomic_write(_path(self.settings, CONFIG_NAME),
                          json.dumps(config, indent=2))
        except Exception as exc:
            self._log("config write failed: %r" % (exc,))
            self.backoff_until = self._clock() + 60
            return False
        _rotate(_path(self.settings, WORKER_LOG_NAME))

        factory = self._process_factory or self._spawn
        try:
            proc = factory(self._worker_args(binary))
        except Exception as exc:
            self._log("worker spawn failed: %r" % (exc,))
            self.proc = None
            self.backoff_until = self._clock() + 60
            return False

        self.proc = proc
        self.started_at = self._clock()
        self.profile = profile
        self.last_switch = self.started_at
        self.gpu_applied = miner_config.gpu_enabled(config)
        self.job_ok = self._assign_job(self.pid)
        self._log("worker started pid=%s gpu=%s profile=%s job=%s"
                  % (self.pid, self.gpu_applied, profile, self.job_ok))
        return True

    def switch(self, profile):
        """Restart the worker under a different profile; False when unchanged."""
        if profile == self.profile:
            return False
        self._log("profile switch %s -> %s" % (self.profile, profile))
        self.stop()
        return self.start(profile)

    def stop(self):
        """Stop the worker gracefully: warm-up wait, ``taskkill``, then hard kill."""
        if not self.alive():
            self.proc = None
            return
        pid = self.pid
        lived = self._clock() - self.started_at
        if lived < self._warmup_seconds:
            self._sleep(max(0.0, self._warmup_seconds - lived))
        try:
            self._terminate(pid)
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
        self._log("worker stopped pid=%s" % pid)

    def reap(self):
        """Detect a dead worker, count it, and arm the next attempt."""
        if self.proc is None or self.proc.poll() is None:
            return
        code = self.proc.returncode
        lived = self._clock() - self.started_at
        self.proc = None

        if lived < FAST_FAIL_SECONDS:
            self.fast_failures += 1
            if self.fast_failures >= 2 and self.use_gpu:
                self.use_gpu = False
                self._log("GPU circuit-breaker: retrying CPU-only")
                self.backoff_until = self._clock() + 30
                return
            base = BACKOFF_GPU_BASE if self.use_gpu else BACKOFF_CPU_BASE
            delay = min(BACKOFF_MAX, base * (2 ** max(0, self.fast_failures - 1)))
            self.backoff_until = self._clock() + delay
            self._log("worker exited fast (code=%s after %.0fs) -> backoff %ds"
                      % (code, lived, delay))
        else:
            self.fast_failures = 0
            self.backoff_until = self._clock() + 5
            self._log("worker exited (code=%s after %.0fs)" % (code, lived))

    # -- telemetry ----------------------------------------------------------

    def read_telemetry(self):
        """Refresh ``self.tele`` from the configured telemetry source."""
        try:
            delta = self._telemetry_source.read()
        except Exception as exc:
            self._log("telemetry read failed: %r" % (exc,))
            return self.tele
        if delta is not None:
            apply_delta(self.tele, delta)
        return self.tele

    def hashrate_text(self):
        """Human hashrate string for ``state.json`` (``"n/a"`` before first line)."""
        return hashrate_text(self.tele)


# ---------------------------------------------------------------------------
# orphan sweep
# ---------------------------------------------------------------------------


def _run(arguments, timeout=45, capture=True):
    """Run a helper executable; returns CompletedProcess-like object or None."""
    try:
        return subprocess.run(
            list(arguments),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=spawn_flags(),
        )
    except Exception:
        return None


def list_worker_pids(settings, runner=None, timeout=45):
    """PIDs of workers running from THIS install directory."""
    install_dir = _install_dir(settings)
    binary = getattr(settings, "binary", DEFAULT_BINARY) or DEFAULT_BINARY
    binary_path = getattr(settings, "bin_path", None) or os.path.join(install_dir, binary)
    stem = os.path.splitext(binary)[0] or "CloudSync"
    script = (
        "Get-Process -Name '%s' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path -eq '%s' } | ForEach-Object { $_.Id }"
        % (stem, binary_path))
    run = runner or _run
    result = run(["powershell.exe", "-NoProfile", "-NonInteractive",
                  "-Command", script], timeout)
    if result is None:
        return []
    pids = []
    for token in (getattr(result, "stdout", b"") or b"").decode("utf-8", "replace").split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def sweep_orphans(settings, keep=None, runner=None, log=None):
    """Kill workers left behind by a previous supervisor; returns count killed.

    A hard-killed controller leaves its child mining, and two miners on one box
    silently halve each other's hashrate.  This is the guard against that.
    """
    killed = 0
    run = runner or _run
    for pid in list_worker_pids(settings, runner=runner):
        if keep and pid == int(keep):
            continue
        try:
            run(["taskkill.exe", "/F", "/PID", str(pid)], 20, capture=False)
            killed += 1
        except Exception:
            pass
    if killed and log:
        try:
            log("swept %d orphan worker(s)" % killed)
        except Exception:
            pass
    return killed
