"""Parse worker telemetry from log lines; API-source ready by design.

The current agent only has the worker's log file (the worker is an xmrig
variant).  This module turns the two interesting line shapes into a small,
pure value object and merges them into the flat ``tele`` dict the supervisor
publishes in ``state.json``::

    speed 10s/60s/15m 3027.0 n/a n/a H/s max 3100.0 H/s
    accepted (5/1) 1234/5000 (24.7%) diff 72000 0ms

...become ``TelemetryDelta(speed_10s=3027.0, unit="H")`` and
``TelemetryDelta(accepted=5, rejected=1)``.

Sources are deliberately abstracted behind :class:`TelemetrySource`: the
:class:`LogTailSource` reads the worker log today; an HTTP-API source (xmrig's
``/2/summary`` endpoint, or any future worker) can be added later as another
class implementing the same one-method interface, with no change to the worker
or the supervisor.

Regexes and the "last match wins" rule are copied verbatim from
``controller.Worker.read_telemetry``.  Everything here is pure: importing the
module does no I/O, and :func:`parse_line` / :func:`parse_text` accept text and
return values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional, Protocol, runtime_checkable

__all__ = [
    "SPEED_RE",
    "SHARES_RE",
    "GPU_READY_RE",
    "TELE_KEYS",
    "TelemetryDelta",
    "TelemetrySource",
    "LogTailSource",
    "parse_line",
    "parse_text",
    "parse_lines",
    "apply_delta",
    "empty_delta",
    "hashrate_text",
    "tail_text",
]

#: ``speed 10s/60s/15m <10s> <60s> <15m> <unit>/s [max <x> <unit>/s]``
SPEED_RE = re.compile(r"speed 10s/60s/15m\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)/s")

#: ``accepted (x/y)`` / ``rejected (x/y)`` - the controller took the last match
#: of either word and mapped groups 1/2 to accepted/rejected, so we do the same.
SHARES_RE = re.compile(r"(?:accepted|rejected) \((\d+)/(\d+)\)")

#: xmrig prints the OpenCL banner as ``opencl   READY`` (alignment spaces).
GPU_READY_RE = re.compile(r"opencl\s+READY")

#: Keys the flat telemetry dict may carry (``Worker.tele`` / state.json).
TELE_KEYS = (
    "speed_10s", "speed_60s", "speed_15m", "unit",
    "accepted", "rejected", "gpu_ready",
)

_NA = frozenset(("n/a", "na", "nan", "none", "-", ""))


def _number(token):
    """Parse one xmrig numeric token; ``None`` for ``n/a``/junk."""
    if token is None:
        return None
    text = str(token).strip().replace(",", "")
    if text.lower() in _NA:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TelemetryDelta:
    """One increment of parsed telemetry.

    Every field is ``None`` unless the parsed line(s) actually carried it, so a
    delta can be merged into an existing ``tele`` dict without clobbering
    counters that did not update this tick.  ``gpu_ready`` is the exception when
    :func:`parse_text` is used: it is always ``True``/``False`` there because
    the original controller re-evaluated it from the whole log tail each read.
    """

    speed_10s: Optional[float] = None
    speed_60s: Optional[float] = None
    speed_15m: Optional[float] = None
    unit: Optional[str] = None
    accepted: Optional[int] = None
    rejected: Optional[int] = None
    gpu_ready: Optional[bool] = None

    # -- convenience --------------------------------------------------------

    def is_empty(self):
        """True when the delta carries nothing at all."""
        return all(value is None for value in self.values())

    def values(self):
        """Tuple of field values in :data:`TELE_KEYS` order."""
        return tuple(getattr(self, key) for key in TELE_KEYS)

    def as_dict(self):
        """Only the non-``None`` fields, keyed by their flat tele names."""
        return {key: getattr(self, key) for key in TELE_KEYS
                if getattr(self, key) is not None}

    def merged_with(self, newer):
        """Return a delta with ``newer``'s non-``None`` fields layered on top."""
        if newer is None:
            return self
        updates = {key: value for key, value in newer.as_dict().items()
                   if key in TELE_KEYS}
        return replace(self, **updates) if updates else self


def empty_delta():
    """A delta with every field unset (used as an accumulator)."""
    return TelemetryDelta()


def _speed_delta(match):
    return TelemetryDelta(
        speed_10s=_number(match.group(1)),
        speed_60s=_number(match.group(2)),
        speed_15m=_number(match.group(3)),
        unit=match.group(4),
    )


def _shares_delta(match):
    return TelemetryDelta(
        accepted=int(match.group(1)),
        rejected=int(match.group(2)),
    )


def parse_line(line):
    """Parse one log line into a :class:`TelemetryDelta`, or ``None``.

    Handles xmrig's hashrate line and share line; a single line only ever
    produces one kind of increment.  The OpenCL ready banner is also detected
    here (``gpu_ready=True``), although :func:`parse_text` is the normal way the
    worker reads it.
    """
    if not line:
        return None
    text = str(line)
    if not text.strip():
        return None

    match = SPEED_RE.search(text)
    if match:
        delta = _speed_delta(match)
        if not delta.is_empty():
            return delta

    match = SHARES_RE.search(text)
    if match:
        return _shares_delta(match)

    if GPU_READY_RE.search(text):
        return TelemetryDelta(gpu_ready=True)

    return None


def parse_lines(lines):
    """Parse an iterable of lines, yielding only the deltas that carry data."""
    for line in lines:
        delta = parse_line(line)
        if delta is not None and not delta.is_empty():
            yield delta


def parse_text(text):
    """Parse a whole log tail: last speed/share line wins, like the controller.

    ``gpu_ready`` is always set (``True`` when the OpenCL banner is present,
    ``False`` otherwise) because the original code re-evaluated it from the tail
    on every read.  Returns ``None`` only when the text carries no telemetry at
    all *and* no GPU banner.

    Every line is tested against all three patterns independently: xmrig (and
    some pool proxies) emit a line that carries the hashrate *and* an
    accepted/rejected count, and the old ``continue`` after the speed match
    dropped the share counters.
    """
    if not text:
        return None
    text = str(text)
    if not text.strip():
        return None

    delta = empty_delta()
    gpu_ready = False
    seen = False
    for line in text.splitlines():
        match = SPEED_RE.search(line)
        if match:
            delta = delta.merged_with(_speed_delta(match))
            seen = True
        match = SHARES_RE.search(line)
        if match:
            delta = delta.merged_with(_shares_delta(match))
            seen = True
        if GPU_READY_RE.search(line):
            gpu_ready = True
            seen = True

    if not seen:
        return None
    if delta.is_empty() and not gpu_ready:
        return None
    return replace(delta, gpu_ready=gpu_ready)


def apply_delta(tele, delta):
    """Merge *delta* into the flat ``tele`` dict; returns the same dict.

    ``None`` fields leave the existing value alone; ``gpu_ready`` overwrites when
    it is set (``parse_text`` always sets it, matching the controller's
    ``tele["gpu_ready"] = ...`` assignment).
    """
    if tele is None:
        tele = {}
    if delta is not None:
        for key, value in delta.as_dict().items():
            tele[key] = value
    return tele


def hashrate_text(tele):
    """``"3027 H/s"`` (or ``"n/a"``) - same formatting as the controller."""
    if not tele:
        return "n/a"
    speed = tele.get("speed_10s")
    if speed is None:
        return "n/a"
    return "%.0f %s/s" % (speed, tele.get("unit") or "H")


def tail_text(path, nbytes=16384):
    """Last ``nbytes`` bytes of a text file, decoded leniently ('' on error)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - int(nbytes)))
            return handle.read().decode("utf-8", "replace")
    except (OSError, TypeError, ValueError):
        return ""


@runtime_checkable
class TelemetrySource(Protocol):
    """Anything that can produce the current telemetry increment.

    Implementations must be cheap and must not raise: return ``None`` when no
    telemetry is available.  A future HTTP source only needs::

        class HttpSummarySource:
            def read(self):
                return parse_summary(request_json(...))
    """

    def read(self):
        """Return a :class:`TelemetryDelta` (or ``None``)."""
        ...  # pragma: no cover - protocol declaration


class LogTailSource:
    """Read the worker log tail and parse it.

    Drop-in replacement for the controller's ``tail_text`` + regex block.  The
    file is re-read on every call (logs are small and this runs at most once per
    supervisor tick); :class:`~cloudsync.core.logging.RotatingLog` is not used
    because the worker owns that file.
    """

    def __init__(self, path, tail_bytes=16384):
        self.path = str(path)
        self.tail_bytes = int(tail_bytes)

    def __repr__(self):
        return "LogTailSource(%r)" % (self.path,)

    def read(self):
        """Parse the current log tail; ``None`` when there is no telemetry."""
        return parse_text(tail_text(self.path, self.tail_bytes))
