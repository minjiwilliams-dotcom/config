"""Per-host configuration overrides: whitelisted, type-checked, deep merge.

The kit has no per-machine configuration today; this module is the safe hook for
one (the SPEC's future "§3.3"). An ``overrides.json`` next to ``install.json``
maps host names to two optional scopes::

    {
      "*":        {"settings": {"idle_seconds": 180},
                   "config":   {"cpu.max-threads-hint": 30}},
      "desk-top": {"config":   {"opencl.enabled": false,
                                "randomx.mode": "light"}}
    }

* Host keys are matched case-insensitively; ``"*"`` (or ``"default"``) is merged
  underneath the host-specific section.
* ``config`` keys are dotted paths into the xmrig config built by
  ``miner.config.build`` (the merge target).
* ``settings`` keys are ``Settings`` attributes the kit allows a host to change.
* Only keys listed in :data:`ALLOWED_CONFIG_PATHS` / :data:`ALLOWED_SETTINGS`
  are accepted and every value is type/range/choice checked. Rejected entries
  are returned as human-readable problems and never applied, so a typo in the
  overrides file cannot switch on an unexpected xmrig option.

No filesystem work happens at import time; :func:`load_overrides` only reads when
called, and a missing/invalid file is an empty override set.
"""

import copy
import json
import os

__all__ = [
    "ALLOWED_CONFIG_PATHS",
    "ALLOWED_SETTINGS",
    "OVERRIDES_FILE",
    "apply_config",
    "load_and_apply",
    "load_overrides",
    "select_host",
    "settings_from_section",
    "validate_section",
]

OVERRIDES_FILE = "overrides.json"

# Rule tuples: (kind, low, high).
#   kind "int"  -> low/high are inclusive bounds (None = unbounded)
#   kind "bool" -> low/high unused
#   kind "str"  -> low is a tuple of accepted values (None = any string)
_INT = "int"
_BOOL = "bool"
_STR = "str"

#: Dotted paths into the generated xmrig config that a host may override.
#: Deliberately excludes pools/wallet, opencl device tables and anything that
#: would let a host escape the kit's pool identity.
ALLOWED_CONFIG_PATHS = {
    "cpu.max-threads-hint": (_INT, 1, 100),
    "cpu.priority": (_INT, 0, 5),
    "cpu.yield": (_BOOL, None, None),
    "pause-on-active": (_INT, 0, 1),
    "print-time": (_INT, 0, 3600),
    "randomx.mode": (_STR, ("auto", "fast", "light"), None),
    "randomx.numa": (_BOOL, None, None),
    "randomx.rdmsr": (_BOOL, None, None),
    "randomx.wrmsr": (_BOOL, None, None),
    "opencl.enabled": (_BOOL, None, None),
}

#: ``Settings`` attributes a host may override (applied by Settings.with_overrides).
ALLOWED_SETTINGS = {
    "idle_seconds": (_INT, 0, 86400),
    "force_gpu": (_BOOL, None, None),
    "remote_update": (_BOOL, None, None),
    "legacy_update": (_BOOL, None, None),
}

_SCOPES = ("config", "settings")


def load_overrides(path):
    """Return the raw overrides mapping; empty dict when absent or invalid."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lookup(mapping, key):
    """Case-insensitive dict lookup; returns {} for a non-mapping value."""
    wanted = key.strip().lower()
    for name, value in mapping.items():
        if isinstance(name, str) and name.strip().lower() == wanted:
            return value if isinstance(value, dict) else {}
    return {}


def _deep_merge(base, patch):
    """Recursively merge *patch* into a copy of *base* (patch wins)."""
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(patch, dict):
        return merged
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def select_host(overrides, hostname=None):
    """Return the merged raw section for *hostname* (``*`` defaults under it).

    ``hostname`` defaults to ``%COMPUTERNAME%``. The result is unvalidated; feed
    it to :func:`validate_section`.
    """
    if not isinstance(overrides, dict) or not overrides:
        return {}
    host = (hostname or os.environ.get("COMPUTERNAME") or "").strip().lower()
    defaults = _lookup(overrides, "*") or _lookup(overrides, "default")
    specific = _lookup(overrides, host) if host else {}
    return _deep_merge(defaults, specific)


def _checked(rule, value):
    """Validate *value* against a rule tuple; raise ValueError when rejected."""
    kind, low, high = rule
    if kind == _BOOL:
        if type(value) is not bool:
            raise ValueError("expected a boolean")
        return value
    if kind == _INT:
        if type(value) is not int:
            raise ValueError("expected an integer")
        if low is not None and value < low:
            raise ValueError("must be >= {0}".format(low))
        if high is not None and value > high:
            raise ValueError("must be <= {0}".format(high))
        return value
    if kind == _STR:
        if not isinstance(value, str):
            raise ValueError("expected a string")
        if low is not None and value not in low:
            raise ValueError("must be one of {0}".format(", ".join(low)))
        return value
    raise ValueError("unknown rule kind {0!r}".format(kind))


def validate_section(section):
    """Validate a host section, returning ``(accepted, problems)``.

    ``accepted`` is ``{"config": {dotted path: value}, "settings": {attribute:
    value}}`` containing only allowed, type-checked entries. ``problems`` lists
    everything that was ignored, in a form suitable for the agent log.
    """
    accepted = {"config": {}, "settings": {}}
    problems = []

    if section is None:
        return accepted, problems
    if not isinstance(section, dict):
        problems.append("overrides section must be an object")
        return accepted, problems

    for scope in section:
        if scope not in _SCOPES:
            problems.append("unknown override scope {0!r}".format(scope))

    for scope, allowed in (
        ("config", ALLOWED_CONFIG_PATHS),
        ("settings", ALLOWED_SETTINGS),
    ):
        values = section.get(scope)
        if values is None:
            continue
        if not isinstance(values, dict):
            problems.append("{0}: expected an object".format(scope))
            continue
        for key, value in values.items():
            rule = allowed.get(key)
            if rule is None:
                problems.append("{0}: key not allowed: {1!r}".format(scope, key))
                continue
            try:
                accepted[scope][key] = _checked(rule, value)
            except ValueError as exc:
                problems.append("{0}.{1}: {2}".format(scope, key, exc))

    return accepted, problems


def _set_path(target, dotted, value):
    """Set *value* at a dotted path, creating intermediate dicts as needed."""
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def apply_config(base, accepted_config):
    """Return a deep copy of *base* with the accepted dotted paths applied."""
    merged = copy.deepcopy(base) if isinstance(base, dict) else {}
    for dotted, value in (accepted_config or {}).items():
        _set_path(merged, dotted, value)
    return merged


def settings_from_section(accepted):
    """Return the validated ``Settings`` attribute overrides from *accepted*."""
    return dict((accepted or {}).get("settings") or {})


def load_and_apply(base, path, hostname=None):
    """One-call convenience: load, validate for host, merge the config.

    Returns ``(merged_config, settings_overrides, problems)``. A missing or
    invalid overrides file yields a deep copy of *base*, empty settings overrides
    and no problems.
    """
    section = select_host(load_overrides(path), hostname)
    accepted, problems = validate_section(section)
    merged = apply_config(base, accepted["config"])
    return merged, settings_from_section(accepted), problems
