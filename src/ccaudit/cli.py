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
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from ccaudit import __version__
from ccaudit.analyse import SessionAnalysis, analyse_transcript
from ccaudit.capture import clear_queue, enqueue, read_queue, release_worker_lock
from ccaudit.config import ccaudit_home, load_pricing, resolve_pricing_path
from ccaudit.config.refresh import DEFAULT_SOURCE_URL, RefreshError, refresh
from ccaudit.ingest.discover import (
    SessionRef,
    discover_sessions,
    fingerprint_transcript,
    latest_session_for_cwd,
    sessions_for_project,
)
from ccaudit.model.policy import DEFAULT_POLICY, POLICIES

# Raised in the model layer, where the invariant lives; re-exported here because this is where
# it becomes exit code 3 (Principle I, Principle X, SC-001).
from ccaudit.model.reconcile import ReconciliationError
from ccaudit.render.data import DEFAULT_GROUPING, GROUPINGS, build_report_data
from ccaudit.render.explain import (
    UnknownFigureError,
    available_figures,
    explain,
    explain_total,
)
from ccaudit.render.terminal import build_console, render_report
from ccaudit.store.db import connect
from ccaudit.store.results import store_result

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
    _add_analysis_options(parser)
    # The metavar is set explicitly so the two internal commands below stay out of the usage
    # line. They are an implementation detail of the plugin hook, not a surface to discover.
    subparsers = parser.add_subparsers(dest="command", metavar="{analyse,sessions,explain,pricing}")

    analyse = subparsers.add_parser(
        "analyse",
        help="Analyse an explicit selection of sessions.",
        aliases=["analyze"],
    )
    _add_analysis_options(analyse)

    sessions = subparsers.add_parser("sessions", help="List the sessions that can be analysed.")
    sessions.add_argument("--project", type=Path, default=None, help="Limit to one project.")
    sessions.add_argument(
        "--all", action="store_true", help="Every session, not just this project."
    )

    explain_parser = subparsers.add_parser(
        "explain",
        help="Show how one figure was derived, down to the records that produced it.",
    )
    explain_parser.add_argument(
        "figure",
        nargs="?",
        default="total",
        help="Figure identifier, or a file path. Defaults to the session total.",
    )
    _add_analysis_options(explain_parser)

    # Internal, invoked by the plugin's SessionEnd hook and by the detached worker it spawns.
    # Underscore-prefixed and help-suppressed: these are not a user-facing surface.
    enqueue_parser = subparsers.add_parser("_enqueue")
    enqueue_parser.add_argument("--session", dest="enqueue_session", default=None)
    enqueue_parser.add_argument("--transcript", dest="enqueue_transcript", default=None)
    subparsers.add_parser("_process_queue")

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


def _add_analysis_options(parser: argparse.ArgumentParser) -> None:
    """Selection and output options, shared by the bare invocation and the subcommands.

    Repeated on the top-level parser on purpose: **zero arguments is a complete invocation**
    (FR-048), and so is `ccaudit --by category` without naming a subcommand.
    """
    parser.add_argument("--session", nargs="+", default=None, help="Explicit session id(s).")
    parser.add_argument("--project", type=Path, default=None, help="All sessions for a project.")
    parser.add_argument("--all", action="store_true", help="Every session in the local corpus.")
    parser.add_argument("--last", type=int, default=None, help="The N most recent in the set.")
    parser.add_argument("--exclude", nargs="+", default=None, help="Drop session id(s) (FR-063).")
    parser.add_argument(
        "--policy",
        choices=POLICIES,
        default=DEFAULT_POLICY,
        help="How shared carry cost is divided among resident items.",
    )
    parser.add_argument(
        "--by",
        dest="group_by",
        choices=GROUPINGS,
        default=DEFAULT_GROUPING,
        help="Group the breakdown by this dimension.",
    )
    parser.add_argument(
        "--top", type=int, default=20, help="Item rows to show; cost is never hidden."
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Redraw as the session progresses. Exits on interrupt or when the session ends.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between coverage checks while watching.",
    )
    parser.add_argument("--redact", action="store_true", help="Obscure paths, keep the structure.")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Translates every failure into its documented exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        if args.command == "pricing":
            return _run_pricing(args, parser)
        if args.command == "_enqueue":
            return _run_enqueue(args)
        if args.command == "_process_queue":
            return _run_process_queue()
        if args.command == "sessions":
            return _run_sessions(args)
        if args.command == "explain":
            return _run_explain(args)
        # Everything else — including the bare, zero-argument invocation — is an analysis.
        return _run_analyse(args)
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


def select_sessions(args: argparse.Namespace) -> list[SessionRef]:
    """Resolve the argument set to sessions on disk.

    With no selection at all this is the zero-argument default: the most recent session of the
    project in the current directory (FR-048). Combining selectors intersects them.
    """
    excluded = set(getattr(args, "exclude", None) or ())

    if getattr(args, "session", None):
        wanted = set(args.session)
        found = [ref for ref in discover_sessions() if ref.session_id in wanted]
        missing = wanted - {ref.session_id for ref in found}
        if missing:
            raise NoSessionsFound(
                f"no local records for session(s): {', '.join(sorted(missing))}. "
                f"Run `ccaudit sessions` to see what is available."
            )
        refs = found
    elif getattr(args, "all", False):
        refs = discover_sessions()
    elif getattr(args, "project", None):
        refs = sessions_for_project(args.project)
    else:
        latest = latest_session_for_cwd()
        if latest is None:
            raise NoSessionsFound(
                "no Claude Code sessions found for this project. Run ccaudit from a directory "
                "where you have used Claude Code, or pass --all to analyse every local session."
            )
        refs = [latest]

    refs = [ref for ref in refs if ref.session_id not in excluded]
    if getattr(args, "last", None):
        refs = refs[: args.last]
    if not refs:
        raise NoSessionsFound("the selection matched no sessions.")
    return refs


def _analyse_selection(args: argparse.Namespace) -> tuple[list[SessionAnalysis], int]:
    """Analyse the selected sessions, returning them and how many were excluded.

    The exclusion count travels with the result because an exclusion is *part of* the answer:
    the flag must never become a silent cherry-picking tool (FR-063).
    """
    refs = select_sessions(args)
    pricing = load_pricing()
    analyses = [
        analyse_transcript(
            ref.path,
            pricing=pricing,
            policy=args.policy,
            project_path=str(ref.project_path) if ref.project_path else None,
            provisional=ref.in_progress,
        )
        for ref in refs
    ]
    return analyses, len(getattr(args, "exclude", None) or ())


def _run_analyse(args: argparse.Namespace) -> int:
    if getattr(args, "watch", False):
        return _run_watch(args)
    analyses, excluded = _analyse_selection(args)
    payload = build_report_data(
        analyses,
        redact=args.redact,
        sessions_excluded_count=excluded,
        group_by=args.group_by,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=False))
        return EXIT_OK
    render_report(payload, console=build_console(), top=args.top)
    return EXIT_OK


def _run_enqueue(args: argparse.Namespace) -> int:
    """Queue a session and return immediately. Never analyses inline (FR-054).

    The session id comes from the flag, or from the environment Claude Code sets for a hook.
    """
    session_id = args.enqueue_session or os.environ.get("CLAUDE_SESSION_ID")
    transcript = args.enqueue_transcript or os.environ.get("CLAUDE_TRANSCRIPT_PATH")
    return enqueue(session_id, transcript)


def _run_process_queue() -> int:
    """Analyse everything queued and store the results. Runs detached, output goes nowhere.

    Every failure is logged rather than raised: this process has no user attached, and a
    crashed worker must leave the queue recoverable rather than the session stuck.
    """
    try:
        entries = read_queue()
        if not entries:
            return EXIT_OK
        pricing = load_pricing()
        connection = connect()
        try:
            known: dict[str, SessionRef] = {ref.session_id: ref for ref in discover_sessions()}
            for entry in entries:
                found = known.get(entry.session_id)
                queued_path = Path(entry.transcript_path) if entry.transcript_path else None
                if found is not None:
                    target, fingerprint = found.path, found.fingerprint
                elif queued_path is not None and queued_path.is_file():
                    target, fingerprint = queued_path, fingerprint_transcript(queued_path)
                else:
                    # The records are gone — the session was deleted between the hook firing
                    # and this worker running. Nothing to analyse and nothing to repair.
                    _LOGGER.warning("queued session %s has no records; skipping", entry.session_id)
                    continue
                analysis = analyse_transcript(target, pricing=pricing)
                store_result(connection, analysis, fingerprint)
                _LOGGER.info("stored analysis for %s", analysis.session_id)
            clear_queue()
        finally:
            connection.close()
    except Exception:
        # Same deviation as `capture.enqueue`, for the same reason: this worker is detached and
        # unattended, so a traceback here reaches nobody. Logged in full; the queue entry
        # survives a crash and the session stays analysable from its records.
        _LOGGER.exception("queue processing failed")
    finally:
        release_worker_lock()
    return EXIT_OK


def _run_watch(args: argparse.Namespace) -> int:
    """Re-analyse and redraw as a session progresses, without the user re-invoking (FR-068).

    Polls the **coverage fingerprint**, not a timer, and redraws only when it actually changes
    — acting on observed state rather than on sleeps is the constitution's rule (Principle VI),
    and it also means a quiet session costs nothing but a stat call.
    """
    if args.json:
        raise UsageError("--watch redraws a terminal view; it cannot be combined with --json.")

    console = build_console()
    seen: str | None = None
    try:
        while True:
            refs = select_sessions(args)
            coverage = "|".join(f"{ref.session_id}:{ref.fingerprint}" for ref in refs)
            if coverage != seen:
                seen = coverage
                analyses, excluded = _analyse_selection(args)
                payload = build_report_data(
                    analyses,
                    redact=args.redact,
                    sessions_excluded_count=excluded,
                    group_by=args.group_by,
                )
                console.clear()
                render_report(payload, console=console, top=args.top)
                console.print("\n[watching — press Ctrl-C to stop]")
            if not any(ref.in_progress for ref in refs):
                console.print("\nThe session has ended; this result is final.")
                return EXIT_OK
            time.sleep(args.interval)
    except KeyboardInterrupt:
        console.print("\nStopped watching.")
        return EXIT_OK


def _run_sessions(args: argparse.Namespace) -> int:
    refs = (
        discover_sessions()
        if args.all or args.project is None
        else sessions_for_project(args.project)
    )
    if args.project is not None and not args.all:
        refs = sessions_for_project(args.project)
    if not refs:
        raise NoSessionsFound("no Claude Code sessions found in the local records.")

    console = build_console()
    console.print(f"{len(refs)} session(s) available\n")
    for ref in refs:
        project = ref.project_path or ref.project_dir
        marker = "  (in progress)" if ref.in_progress else ""
        console.print(
            f"{ref.session_id}  {ref.modified_at:%Y-%m-%d %H:%M}  "
            f"{ref.record_count:>6,} records  {ref.byte_size / 1e6:>6.1f} MB  {project}{marker}"
        )
    return EXIT_OK


def _run_explain(args: argparse.Namespace) -> int:
    analyses, _ = _analyse_selection(args)
    if len(analyses) > 1:
        raise UsageError(
            f"explain works on one session at a time; the selection matched {len(analyses)}. "
            f"Pass --session with a single id."
        )
    analysis = analyses[0]
    console = build_console()

    if args.figure in ("total", "session"):
        console.print(explain_total(analysis).render())
        return EXIT_OK
    try:
        console.print(explain(analysis, args.figure).render())
    except UnknownFigureError as exc:
        # Not an error state: the caller asked for something that is not there, and the useful
        # response is the list of what is.
        print(f"ccaudit: {exc}", file=sys.stderr)
        print(
            "\nAvailable figures:\n  " + "\n  ".join(available_figures(analysis)[:40]),
            file=sys.stderr,
        )
        return EXIT_USAGE
    return EXIT_OK


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
