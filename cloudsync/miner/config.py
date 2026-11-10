"""Build the worker configuration (``config.dat``) from profile + settings.

This is the merge that used to live in ``controller.build_config``:

1. load ``config_high.json`` (the published template) and deep-copy it;
2. substitute the wallet into the pool user and set the pool password to the
   machine's hostname (per-rig identity is the hostname, unchanged from the
   original kit);
3. add the OpenCL/CUDA block from ``gpu_profile.json`` only when the profile
   allows the GPU *and* a GPU profile is available (the TDR guard is the
   caller's job, injected as ``driver_reset_recently``);
4. RandomX NUMA, light mode + 25 % CPU hint under 6 GB RAM;
5. MSR read/write only when elevated *and* ``WinRing0x64.sys`` sits next to the
   binary (same rule as the controller);
6. fixed worker switches (``pause-on-active`` 0, ``print-time`` 30,
   ``autosave``/``watch``/``colors``/``title`` off);
7. apply the CPU shape from the selected profile (``hint`` / ``priority`` /
   ``yield``) last, so a switch is deterministic.

The function takes every input it cannot derive cheaply as an optional
parameter, which keeps it unit-testable without a Windows box or a real install
directory.  ``build()`` is the public contract (SPEC): ``build(settings,
profile, gpu_profile) -> dict``.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping

from ..config.profiles import PROFILES, get_profile
from ..config.settings import read_json

__all__ = [
    "PROFILES",
    "CONFIG_NAME",
    "WALLET_NAME",
    "GPU_SYS_NAME",
    "LOW_RAM_GB",
    "FIXED_SETTINGS",
    "build",
    "gpu_enabled",
    "load_base_config",
    "read_wallet",
]

CONFIG_NAME = "config_high.json"
WALLET_NAME = "wallet.txt"
GPU_SYS_NAME = "WinRing0x64.sys"
LOW_RAM_GB = 6.0

#: Switches the controller forced on every generated config (parity).
FIXED_SETTINGS = {
    "pause-on-active": 0,
    "print-time": 30,
    "autosave": False,
    "watch": False,      # no live reload: RandomX reload is broken in 6.24
    "colors": False,
    "title": False,
}

_WALLET_PLACEHOLDER = "__WALLET__"


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


def _install_dir(settings):
    """Best-effort install directory for a settings object or a plain path."""
    if settings is None:
        return os.getcwd()
    if isinstance(settings, (str, bytes, os.PathLike)):
        return os.fspath(settings)
    return getattr(settings, "install_dir", None) or os.getcwd()


def _path(settings, name):
    """``settings.path(name)`` when available, else ``<install_dir>/<name>``."""
    if settings is not None and not isinstance(settings, (str, bytes, os.PathLike)):
        path_helper = getattr(settings, "path", None)
        if callable(path_helper):
            try:
                return path_helper(name)
            except Exception:
                pass
    return os.path.join(_install_dir(settings), name)


def load_base_config(settings=None, base=None, path=None):
    """Load the ``config_high.json`` template; raise :class:`ConfigError` if bad.

    ``base`` short-circuits the read (unit tests / cached copies).  A path may be
    given explicitly; otherwise it is ``<install_dir>/config_high.json``.
    """
    from ..core.errors import ConfigError

    if base is not None:
        if not isinstance(base, Mapping):
            raise ConfigError("config_high.json unreadable")
        return base
    target = path or _path(settings, CONFIG_NAME)
    loaded = read_json(target, None)
    if not isinstance(loaded, Mapping):
        raise ConfigError("config_high.json unreadable")
    return loaded


def read_wallet(settings=None, wallet=None, path=None):
    """Return the wallet address (``wallet.txt``, UTF-8 with optional BOM)."""
    from ..core.errors import ConfigError

    if wallet is not None:
        text = str(wallet).strip()
    else:
        target = path or _path(settings, WALLET_NAME)
        try:
            with open(target, "r", encoding="utf-8-sig") as handle:
                text = handle.read().strip()
        except OSError as exc:
            raise ConfigError("wallet.txt unreadable") from exc
    if not text:
        raise ConfigError("wallet.txt empty")
    return text


def _profile_values(profile):
    """Normalise a profile name / Profile dataclass / mapping to a dict."""
    if profile is None:
        profile = "full"
    if isinstance(profile, str):
        profile = get_profile(profile)
    as_dict = getattr(profile, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    if isinstance(profile, Mapping):
        return dict(profile)
    return {
        "hint": int(getattr(profile, "hint", PROFILES["full"].hint)),
        "priority": int(getattr(profile, "priority", PROFILES["full"].priority)),
        "yield": bool(getattr(profile, "yield_", getattr(profile, "yield", False))),
        "gpu": bool(getattr(profile, "gpu", True)),
    }


def _device_count(opencl):
    """Best-effort count of OpenCL devices in a profile (for the log line)."""
    try:
        return len(opencl.get("rx/0") or opencl.get("devices") or [])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build(settings=None, profile="full", gpu_profile=None, *,
          use_gpu=True,
          base=None,
          wallet=None,
          hostname=None,
          ram_gb=None,
          is_admin=None,
          sys_file_present=None,
          driver_reset_recently=False,
          log=None):
    """Return the worker configuration dict (the body of ``config.dat``).

    Parameters match ``controller.build_config(settings, use_gpu, profile)`` but
    with the profile first, as the SPEC contract says.  ``gpu_profile`` is the
    parsed ``gpu_profile.json`` mapping (``None`` = CPU only).  All the machine
    probes are injectable so the merge is testable off-box.
    """
    values = _profile_values(profile)

    cfg = copy.deepcopy(dict(load_base_config(settings, base=base)))

    # --- pool identity: wallet + hostname ----------------------------------
    host = hostname or os.environ.get("COMPUTERNAME") or "host"
    pools = list(cfg.get("pools") or [{}])
    if not pools:
        pools = [{}]
    pool = dict(pools[0] or {})
    user = pool.get("user") or _WALLET_PLACEHOLDER + ".Worker1"
    pool["user"] = str(user).replace(_WALLET_PLACEHOLDER, read_wallet(settings, wallet))
    pool["pass"] = host
    pools[0] = pool
    cfg["pools"] = pools

    # --- GPU block ---------------------------------------------------------
    if is_admin is None:
        is_admin = _default_is_admin()
    if sys_file_present is None:
        try:
            sys_file_present = os.path.isfile(_path(settings, GPU_SYS_NAME))
        except Exception:
            sys_file_present = False
    if ram_gb is None:
        ram_gb = _default_ram_gb()

    opencl = (gpu_profile or {}).get("opencl") if isinstance(gpu_profile, Mapping) else None
    gpu_ok = bool(use_gpu and values["gpu"] and isinstance(opencl, Mapping)
                  and opencl.get("enabled"))
    if gpu_ok and not getattr(settings, "force_gpu", False) and driver_reset_recently:
        gpu_ok = False
        _log(log, "GPU disabled for this run (recent driver reset)")
    if gpu_ok:
        cfg["opencl"] = dict(opencl)
        cuda = (gpu_profile or {}).get("cuda")
        if cuda:
            cfg["cuda"] = cuda
        _log(log, "GPU backend enabled (platform %s, %d device(s))"
                  % (opencl.get("platform"), _device_count(opencl)))
    else:
        cfg["opencl"] = {"enabled": False}
        if not gpu_profile:
            _log(log, "no GPU profile; CPU-only")

    # --- RandomX: NUMA, low-RAM light mode, MSR ----------------------------
    randomx = cfg.setdefault("randomx", {})
    randomx["numa"] = True
    if float(ram_gb) < LOW_RAM_GB:
        randomx["mode"] = "light"
        cfg.setdefault("cpu", {})["max-threads-hint"] = 25
        _log(log, "low RAM (%.1f GB): RandomX light mode" % float(ram_gb))
    msr = bool(is_admin and sys_file_present)
    randomx["rdmsr"] = msr
    randomx["wrmsr"] = msr

    # --- fixed switches ----------------------------------------------------
    cfg.update(FIXED_SETTINGS)

    # --- CPU shape last (profile wins over the template) -------------------
    cpu = cfg.setdefault("cpu", {})
    cpu["max-threads-hint"] = values["hint"]
    cpu["priority"] = values["priority"]
    cpu["yield"] = values["yield"]
    return cfg


def gpu_enabled(cfg):
    """True when a built config keeps the GPU block enabled."""
    opencl = (cfg or {}).get("opencl") if isinstance(cfg, Mapping) else None
    return bool(isinstance(opencl, Mapping) and opencl.get("enabled"))


# ---------------------------------------------------------------------------
# default probes (lazy imports keep this module importable everywhere)
# ---------------------------------------------------------------------------


def _default_is_admin():
    try:
        from ..core.clock import is_admin
        return bool(is_admin())
    except Exception:
        return False


def _default_ram_gb():
    try:
        from ..core.clock import total_ram_gb
        return float(total_ram_gb())
    except Exception:
        return 8.0


def _log(log, message):
    """Best-effort log: callable, object with ``log``/``write``, or None."""
    if not log:
        return
    try:
        if callable(log):
            log(message)
            return
        for name in ("log", "write"):
            method = getattr(log, name, None)
            if callable(method):
                method(message)
                return
    except Exception:
        pass
