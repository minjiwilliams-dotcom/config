"""Configuration layer for the CloudSync agent.

Three small, side-effect-free modules:

* :mod:`cloudsync.config.settings`  -- install/manifest settings and paths
* :mod:`cloudsync.config.profiles`  -- worker profiles + GPU profile loading
* :mod:`cloudsync.config.overrides` -- per-host overrides (whitelisted merge)

Importing this package does no work beyond importing the three modules; none of
them touch the filesystem until a class or function is called.
"""

from . import overrides, profiles, settings

__all__ = ["settings", "profiles", "overrides"]
