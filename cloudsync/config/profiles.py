"""Worker profiles and GPU-profile loading.

Mirrors ``PROFILES`` / ``load_profile`` in ``F:\\Test Miner\\controller.py``.
A profile is pure data: the CPU shape (``hint`` / ``priority`` / ``yield``) and
whether the GPU block is allowed at all. Switching profiles is a graceful
worker restart, because xmrig 6.24's ``watch`` reload leaves RandomX broken.

Importing this module does no filesystem work; only the functions read files.
"""

from dataclasses import dataclass
from typing import Iterator, Mapping

from .settings import DEFAULT_IDLE_SECONDS, read_json

__all__ = [
    "DEFAULT_PROFILE",
    "GPU_PROFILE_SCHEMA",
    "PROFILES",
    "Profile",
    "choose_profile",
    "get_profile",
    "load_gpu_profile",
]

GPU_PROFILE_SCHEMA = 1
DEFAULT_PROFILE = "full"

_PROFILE_FIELDS = ("hint", "priority", "yield", "gpu")


@dataclass(frozen=True)
class Profile(Mapping):
    """One worker profile.

    Usable both as a dataclass (``prof.hint``) and as the original controller
    dict (``prof["yield"]``) so callers ported from ``controller.py`` keep
    working. ``yield`` is a Python keyword, hence the ``yield_`` field name.
    """

    name: str
    hint: int
    priority: int
    yield_: bool
    gpu: bool

    def as_dict(self):
        """Return the field values keyed by their controller names."""
        return {
            "hint": self.hint,
            "priority": self.priority,
            "yield": self.yield_,
            "gpu": self.gpu,
        }

    def __getitem__(self, key):
        if key not in _PROFILE_FIELDS:
            raise KeyError(key)
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(_PROFILE_FIELDS)

    def __len__(self) -> int:
        return len(_PROFILE_FIELDS)


#: The two profiles the kit ships: full (user away, GPU allowed) and trickle
#: (user present, CPU only, yields to interactive work).
PROFILES = {
    "full": Profile("full", hint=50, priority=2, yield_=False, gpu=True),
    "trickle": Profile("trickle", hint=25, priority=1, yield_=True, gpu=False),
    # headless/session-0 only: every thread, no yield. Selected by the supervisor
    "max": Profile("max", hint=100, priority=2, yield_=False, gpu=True),
}


def get_profile(name):
    """Return the named profile, falling back to ``full`` like the controller."""
    return PROFILES.get(name) or PROFILES[DEFAULT_PROFILE]


def choose_profile(idle, user_active, idle_threshold=DEFAULT_IDLE_SECONDS):
    """Pick a profile name from the user state.

    A present user always gets ``trickle``; otherwise ``full`` is used only once
    the machine has been idle for at least *idle_threshold* seconds. This mirrors
    the controller's "mine only when idle" gate without the job-object shortcut.
    """
    if user_active:
        return "trickle"
    return DEFAULT_PROFILE if idle >= idle_threshold else "trickle"


def load_gpu_profile(path):
    """Return the ``gpu_profile.json`` mapping when it is schema v1, else None.

    Missing, unreadable, non-mapping or wrong-schema files all return ``None``
    (the caller then runs CPU-only), matching ``controller.load_profile``.
    """
    profile = read_json(path, None)
    if not isinstance(profile, dict):
        return None
    if profile.get("schema") != GPU_PROFILE_SCHEMA:
        return None
    return profile
