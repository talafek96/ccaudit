"""The command-line surface — the primary and only mandatory interface.

Every figure available anywhere is obtainable here (FR-074), and **zero arguments is a
complete invocation**: analyse the most recent session of the project in the current directory
and print the summary, with no config file, no account, and no setup step (FR-048, FR-050).

**Exit code 3 has its own code on purpose.** Every other failure is visible — a crash, a
missing file, a bad argument. A breakdown that does not add up produces a complete,
plausible-looking report full of wrong numbers, which is worse than no report because someone
will act on it. So it is a distinct code that can never be mistaken for a warning.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from ccaudit import __version__
from ccaudit.config import ccaudit_home, load_pricing, resolve_pricing_path
from ccaudit.config.refresh import DEFAULT_SOURCE_URL, RefreshError, refresh

# Raised in the model layer, where the invariant lives; re-exported here because this is where
# it becomes exit code 3 (Principle I, Principle X, SC-001).
from ccaudit.model.reconcile import ReconciliationError

# The exit-code contract from contracts/cli.md. These are part of the interface: a script that
# branches on them must keep working, so they are named constants, not literals at call sites.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_SESSIONS = 2
EXIT_DOES_NOT_ADD_UP = 3
EXIT_DATA_ERROR = 4
EXIT_INTERRUPTED = 130

LOG_FILENAME = "ccaudit.log"
_LOGGER = logging.getLogger("ccaudit")


class UsageError(ValueError):
    """A bad argument or an impossible selection. Exits 1."""


class NoSessionsFound(LookupError):
    """The selection matched nothing analysable. Exits 2 — not an error, just empty."""


class DataError(RuntimeError):
    """Records are unreadable in a way that prevents a result. Exits 4; names the record."""


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that respects this tool's exit-code contract.

    argparse exits **2** on a bad argument, and 2 already means "no analysable sessions found"
    here — a normal, empty outcome a script may reasonably ignore. Left alone, a typo in a flag
    would be indistinguishable from a clean empty result. Usage errors exit 1.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")
        raise AssertionError("unreachable: ArgumentParser.exit always raises SystemExit")


def build_parser() -> argparse.ArgumentParser:
    """The command surface from contracts/cli.md."""
    parser = _Parser(
        prog="ccaudit",
        description=(
            "Where did this Claude Code session's money go? Reports API-equivalent cost "
            "estimates per file, per folder, and per category — never a billed amount."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ccaudit {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="raise log detail; repeat for debug.",
    )
    subparsers = parser.add_subparsers(dest="command")

    pricing = subparsers.add_parser(
        "pricing",
        help="Show or update the rate table figures are imputed from.",
        description=(
            "Rates, cache multipliers, and cacheability thresholds are not pinned to the "
            "installed version of ccaudit. They live in a table you own, under CCAUDIT_HOME, "
            "which survives upgrades."
        ),
    )
    pricing_sub = pricing.add_subparsers(dest="pricing_command")
    pricing_sub.add_parser("show", help="Which table is in effect, and how old its rates are.")
    refresh_parser = pricing_sub.add_parser(
        "refresh",
        help="Update the rate table from a public source. The only command that uses the network.",
    )
    refresh_parser.add_argument(
        "--source-url",
        default=None,
        help="Rate source to fetch. Defaults to the public LiteLLM rate table.",
    )
    refresh_parser.add_argument(
        "--from",
        dest="source_file",
        type=Path,
        default=None,
        help="Read rates from a local file instead of the network, for an air-gapped machine.",
    )
    refresh_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Translates every failure into its documented exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        if args.command == "pricing":
            return _run_pricing(args, parser)
        # Analysis commands land here as they are implemented; the zero-argument default
        # (FR-048) is the next one to arrive.
        parser.print_help()
        return EXIT_OK
    except UsageError as exc:
        print(f"ccaudit: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except NoSessionsFound as exc:
        print(f"ccaudit: {exc}", file=sys.stderr)
        return EXIT_NO_SESSIONS
    except ReconciliationError as exc:
        print(f"ccaudit: the breakdown does not add up: {exc}", file=sys.stderr)
        print(
            "Refusing to print figures that contradict their own total. "
            "This is a defect in ccaudit, not in your session.",
            file=sys.stderr,
        )
        return EXIT_DOES_NOT_ADD_UP
    except DataError as exc:
        print(f"ccaudit: {exc}", file=sys.stderr)
        return EXIT_DATA_ERROR
    except KeyboardInterrupt:
        print("ccaudit: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


def configure_logging(verbosity: int = 0) -> None:
    """Wire the constitution's log levels to stderr and to a file.

    Console stays quiet by default so the report is the output; the file target exists so a
    failure in the hook path can be logged rather than surfaced into the user's session
    (FR-054).
    """
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    _LOGGER.setLevel(logging.DEBUG)
    _LOGGER.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    _LOGGER.addHandler(console)

    try:
        home = ccaudit_home()
        home.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(home / LOG_FILENAME, encoding="utf-8")
    except OSError:
        # An unwritable state directory must not stop an analysis that needs nothing from it.
        # Console logging still carries everything the user sees.
        return
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _LOGGER.addHandler(file_handler)


def _run_pricing(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.pricing_command == "show":
        return _pricing_show()
    if args.pricing_command == "refresh":
        return _pricing_refresh(args)
    parser.parse_args(["pricing", "--help"])
    return EXIT_USAGE


def _pricing_show() -> int:
    path, origin = resolve_pricing_path()
    pricing = load_pricing()
    print(f"table:    {path}")
    print(f"origin:   {origin}")
    print(f"rates as published: {pricing.priced_on}")
    print(f"models:   {len(pricing.models)}")
    print(
        f"cache:    read x{float(pricing.cache.read)}, "
        f"write x{float(pricing.cache.write_5m)} at 5m, x{float(pricing.cache.write_1h)} at 1h"
    )
    missing = sorted(m for m, p in pricing.models.items() if p.min_cacheable_tokens is None)
    if missing:
        print(
            f"\nmissing min_cacheable_tokens (must be filled in by hand; cannot be derived "
            f"from the model ordering): {', '.join(missing)}"
        )
    print(
        "\nFigures are API-equivalent cost estimates imputed from these rates. "
        "They are not billed amounts."
    )
    if origin == "bundled":
        print(
            f"\nThese are the rates shipped with this build. To update them without "
            f"upgrading:\n  ccaudit pricing refresh\nwhich writes to {ccaudit_home()}."
        )
    return EXIT_OK


def _pricing_refresh(args: argparse.Namespace) -> int:
    if args.source_url and args.source_file:
        raise UsageError("--source-url and --from are alternatives; pass one, not both.")
    try:
        report = refresh(
            source_url=args.source_url or DEFAULT_SOURCE_URL,
            source_file=args.source_file,
            dry_run=args.dry_run,
        )
    except RefreshError as exc:
        raise DataError(f"pricing refresh failed, existing rates left unchanged: {exc}") from exc

    for line in report.lines():
        print(line)
    if args.dry_run:
        print("\n(dry run — nothing written)")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())
