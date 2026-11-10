"""CloudSync miner agent (modular package).

The distribution version lives here so ``pyproject.toml`` can read it without
importing anything else; every other module is imported only when used.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
