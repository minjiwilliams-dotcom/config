"""GPU probe: invoke ``gpudetect.py`` and load ``gpu_profile.json``.

The current controller never parses GPU details itself.  It runs the sibling
probe script once, exactly as the installer does::

    <python> gpudetect.py --force      (cwd = install dir, 120 s timeout,
                                        CREATE_NO_WINDOW, output discarded)

and then treats the mere existence of ``gpu_profile.json`` as success (the
probe exits 3 when no OpenCL device exists, which is not an error for us).
:func:`ensure_profile` mirrors that behaviour; :func:`load_profile` mirrors the
schema check (``schema == 1``, otherwise no profile).

Nothing runs at import time and no probe is spawned unless explicitly asked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from ..platform import winapi

__all__ = [
    "PROFILE_NAME", "DETECT_NAME", "SCHEMA", "PROBE_TIMEOUT",
    "profile_path", "detect_path", "is_available", "load_profile",
    "read_profile", "valid", "run_probe", "ensure_profile",
    "ensure_profile_and_load", "ensure_gpu_profile",
]

PROFILE_NAME = "gpu_profile.json"
DETECT_NAME = "gpudetect.py"
SCHEMA = 1
PROBE_TIMEOUT = 120


def profile_path(install_dir):
    """Full path of ``gpu_profile.json`` inside the install directory."""
    return os.path.join(os.fspath(install_dir), PROFILE_NAME)


def detect_path(install_dir):
    """Full path of the ``gpudetect.py`` probe script."""
    return os.path.join(os.fspath(install_dir), DETECT_NAME)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:
        return None


def valid(profile):
    """True when ``profile`` is a usable gpu_profile document (schema 1)."""
    return isinstance(profile, dict) and profile.get("schema") == SCHEMA


def read_profile(path):
    """Load a profile from an explicit file path; None when missing/invalid."""
    profile = _read_json(path)
    return profile if valid(profile) else None


def load_profile(source):
    """Load the GPU profile.

    ``source`` may be the install directory or the profile file itself.
    Returns the dict when the schema matches, else None (same contract as
    ``controller.load_profile``).
    """
    path = os.fspath(source)
    if os.path.isdir(path) or not path.lower().endswith(".json"):
        path = profile_path(path)
    return read_profile(path)


def is_available(install_dir):
    """True when a gpu_profile.json already exists in the install directory."""
    return os.path.isfile(profile_path(install_dir))


def run_probe(install_dir, python=None, force=True, timeout=PROBE_TIMEOUT):
    """Run ``gpudetect.py`` once; True when a gpu_profile.json now exists.

    The probe's exit code is deliberately ignored: it returns 3 when the box
    has no usable OpenCL device, which still writes a valid CPU-only profile.
    """
    install_dir = os.fspath(install_dir)
    script = detect_path(install_dir)
    if not os.path.isfile(script):
        return False
    executable = python or sys.executable
    arguments = [executable, script]
    if force:
        arguments.append("--force")
    try:
        subprocess.run(
            arguments,
            cwd=install_dir,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=winapi.creation_flags(),
        )
    except Exception:
        return False
    return is_available(install_dir)


def ensure_profile(install_dir, python=None, force=False,
                   timeout=PROBE_TIMEOUT):
    """Make sure a profile exists; run the probe only when needed.

    ``force=False`` (default) keeps an existing profile.  The probe itself is
    always invoked with ``--force``, matching ``controller.ensure_gpu_profile``.
    """
    if not force and is_available(install_dir):
        return True
    return run_probe(install_dir, python=python, force=True, timeout=timeout)


def ensure_profile_and_load(install_dir, python=None, force=False,
                            timeout=PROBE_TIMEOUT):
    """Ensure a profile exists and return it (None when unavailable)."""
    if not ensure_profile(install_dir, python=python, force=force,
                          timeout=timeout):
        return None
    return load_profile(install_dir)


# Compatibility alias: the current controller's function is ensure_gpu_profile().
ensure_gpu_profile = ensure_profile
