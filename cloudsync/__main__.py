"""Entry point for ``python -m cloudsync``.

``--version`` is answered locally (the supervisor module is imported lazily), so
the package stays usable while the rest of the refactor is still landing. Every
other invocation delegates to :func:`cloudsync.supervisor.main`.
"""

import sys

from . import __version__

__all__ = ["main"]


def main(argv=None):
    """Run the supervisor and return the process exit code.

    ``argv`` defaults to ``sys.argv[1:]`` and may be passed by tests. Only
    ``--version`` / ``-V`` are handled here; the supervisor owns everything else.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args or "-V" in args:
        print("cloudsync " + __version__)
        return 0

    from . import supervisor

    result = supervisor.main()
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
