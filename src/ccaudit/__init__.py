"""ccaudit — local-first cost observability for Claude Code sessions."""

from importlib.metadata import PackageNotFoundError, version

# Derived from the git tag at build time (hatch-vcs), read back here from the installed
# metadata. There is deliberately no literal to edit: a number in a file and a tag in git are
# two facts that drift, and this project already shipped a bug that hid behind exactly that —
# a cache keyed on a version nobody ever bumped (see store/cache.py).
#
# A release built from `v0.2.0` is `0.2.0` on PyPI, under `uvx`, and here. A build from any
# other commit says so: `0.2.1.dev4+g1a2b3c` names how far past the tag it is and which commit
# it came from, which is the question "what am I actually running" that a frozen version could
# never answer.
try:
    __version__ = version("ccaudit")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    # Importable without being installed: the version is the one thing that genuinely cannot
    # be known here, and refusing to import over it would be worse than saying so.
    __version__ = "0.0.0+unknown"
