"""The command-line surface — the primary and only mandatory interface.

Every figure available anywhere is obtainable here (FR-074), and **zero arguments is a
complete invocation**: analyse every session of the project in the current directory and print
the summary, with no config file, no account, and no setup step (FR-048, FR-050).

**Exit code 3 has its own code on purpose.** Every other failure is visible — a crash, a
missing file, a bad argument. A breakdown that does not add up produces a complete,
plausible-looking report full of wrong numbers, which is worse than no report because someone
will act on it. So it is a distinct code that can never be mistaken for a warning.
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import webbrowser
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, NoReturn

from ccaudit import __version__
from ccaudit.analyse import (
    ReportInput,
    SessionAnalysis,
    analyse_transcript,
    contribution_of,
)
from ccaudit.capture import clear_queue, enqueue, read_queue, release_worker_lock
from ccaudit.config import (
    UnknownModelError,
    ccaudit_home,
    load_pricing,
    resolve_pricing_path,
)
from ccaudit.config.refresh import DEFAULT_SOURCE_URL, RefreshError, refresh
from ccaudit.footprint import measure as measure_footprint
from ccaudit.ingest.discover import (
    SessionRef,
    discover_sessions,
    fingerprint_transcript,
    sessions_for_cwd,
    sessions_for_project,
)
from ccaudit.model.policy import DEFAULT_POLICY, POLICIES

# Raised in the model layer, where the invariant lives; re-exported here because this is where
# it becomes exit code 3 (Principle I, Principle X, SC-001).
from ccaudit.model.reconcile import ReconciliationError
from ccaudit.notebook import DEFAULT_NOTEBOOK, write_notebook
from ccaudit.render.data import (
    DEFAULT_GROUPING,
    DEFAULT_SORT,
    GROUPINGS,
    SORTS,
    build_report_data,
)
from ccaudit.render.explain import (
    UnknownFigureError,
    available_figures,
    explain,
    explain_total,
)
from ccaudit.render.report import write_report
from ccaudit.render.serve import Selection, serve_ui
from ccaudit.render.terminal import build_console, render_report
from ccaudit.store.cache import cache_key, read_contribution, store_contribution
from ccaudit.store.db import SchemaVersionError, connect
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

# Escape hatch used by the tests that prove the store is a cache and nothing more: the same
# corpus analysed with and without it must produce identical figures (FR-110).
NO_CACHE_ENV = "CCAUDIT_NO_CACHE"
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
    sessions.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable listing, for scripts and for the notebook.",
    )

    ui_parser = subparsers.add_parser(
        "ui",
        help="Explore the breakdown in a browser. Loopback only; leaves nothing running.",
    )
    ui_parser.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="Print the URL instead of opening a browser.",
    )
    _add_analysis_options(ui_parser)

    footprint_parser = subparsers.add_parser(
        "footprint",
        help="What ccaudit itself cost this session. It measures its own contribution.",
    )
    _add_analysis_options(footprint_parser)

    report_parser = subparsers.add_parser(
        "report",
        help="Write a self-contained HTML report that opens offline, anywhere.",
    )
    report_parser.add_argument(
        "--out",
        type=Path,
        default=Path("ccaudit-report.html"),
        help="Where to write the report.",
    )
    report_parser.add_argument(
        "--open",
        dest="open_report",
        action="store_true",
        help="Open the report once written.",
    )
    _add_analysis_options(report_parser)

    notebook_parser = subparsers.add_parser(
        "notebook",
        help="Write a marimo notebook for exploring the data interactively.",
        description=(
            "Writes a marimo notebook — a plain Python file — that explores this data "
            "interactively. ccaudit does not depend on marimo: the notebook declares what it "
            "needs inline, so `uvx marimo edit --sandbox <file>` builds a throwaway "
            "environment for it and installs nothing on your machine. Its cells call ccaudit "
            "for every figure, so the notebook and the terminal cannot disagree."
        ),
    )
    notebook_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the notebook here and keep it, instead of opening a throwaway one.",
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
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Only the most recent session of the selection (default: all of this project's).",
    )
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
        "--sort",
        dest="sort_by",
        choices=SORTS,
        default=DEFAULT_SORT,
        help="Ranking measure. Reorders rows; never changes what they sum to.",
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
        if args.command == "ui":
            return _run_ui(args)
        if args.command == "footprint":
            return _run_footprint(args)
        if args.command == "report":
            return _run_report(args)
        if args.command == "notebook":
            return _run_notebook(args)
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

    With no selection at all this is the zero-argument default: every session of the project in
    the current directory (FR-048). Combining selectors intersects them.

    The default is the *project*, not the latest session, because the question the tool answers
    — where does the money go — is one about a body of work, and a single session answers it
    only by accident. ``--latest`` narrows to one; ``--all`` widens to the machine.
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
        refs = sessions_for_cwd()
        if not refs:
            raise NoSessionsFound(
                "no Claude Code sessions found for this project. Run ccaudit from a directory "
                "where you have used Claude Code, or pass --all to analyse every local session."
            )

    refs = [ref for ref in refs if ref.session_id not in excluded]
    # Narrowing happens after exclusion, so `--latest --exclude <newest>` means the newest one
    # you did not exclude, rather than nothing at all.
    if getattr(args, "latest", False):
        refs = refs[:1]
    if getattr(args, "last", None):
        refs = refs[: args.last]
    if not refs:
        raise NoSessionsFound("the selection matched no sessions.")
    return refs


@dataclass(frozen=True)
class Selected:
    """What a selection produced, including how it produced it.

    ``recalled`` is part of the answer rather than a statistic: a figure's provenance includes
    whether it was recalled or derived (FR-109). A reader who sees a corpus total appear
    instantly is entitled to know it was not recomputed.
    """

    analyses: list[ReportInput]
    excluded: int
    skipped: list[str]
    recalled: int = 0


def _analyse_selection(args: argparse.Namespace, *, cached: bool = True) -> Selected:
    """Analyse the selected sessions, returning them, how many were excluded, and what was skipped.

    The exclusion count travels with the result because an exclusion is *part of* the answer:
    the flag must never become a silent cherry-picking tool (FR-063).

    **Unpriceable sessions are skipped, not fatal — but only in a sweep.** ``~/.claude`` is a
    shared directory: other tools write their own sessions into it, and a session run against a
    model this rate table does not cover cannot be priced at all. Killing a 200-session sweep
    over one foreign session would be the wrong failure — the answer for the other 199 is still
    correct and still reconciles. So a sweep names what it skipped and carries on.

    Fail-fast still governs the case where the reader **named** the session (Principle I):
    ``--session <id>`` is a question about that session, and answering it with a silent skip
    would hide the reason. A sweep is a different question — "what did everything cost" — and
    there the honest answer is the total for what could be priced, plus what was left out.
    """
    refs = select_sessions(args)
    named = bool(getattr(args, "session", None))
    pricing = load_pricing()
    analyses: list[ReportInput] = []
    skipped: list[str] = []
    recalled = 0
    with _result_cache(enabled=cached) as cache:
        for ref in refs:
            key = (
                cache_key(ref.session_id, ref.fingerprint, args.policy, pricing.fingerprint)
                if cache is not None and not ref.in_progress
                else None
            )
            # An in-progress session is never served from the store: its records are still
            # growing, so a cached figure for it is a figure for a session that no longer
            # exists (FR-108).
            stored = read_contribution(cache, key) if cache is not None and key else None
            if stored is not None:
                analyses.append(stored)
                recalled += 1
                continue
            try:
                analysis = analyse_transcript(
                    ref.path,
                    pricing=pricing,
                    policy=args.policy,
                    project_path=str(ref.project_path) if ref.project_path else None,
                    provisional=ref.in_progress,
                    # Supplied rather than re-read: discovery already found it on the pass that
                    # fingerprinted the file, and reading the transcript twice to name it would
                    # undo the work that made a corpus sweep affordable.
                    title=ref.title,
                )
            except UnknownModelError as exc:
                if named:
                    raise
                skipped.append(f"{ref.session_id} ({exc.model})")
                continue
            analyses.append(analysis)
            if cache is not None and key is not None:
                # Best-effort: the cache is not a source of truth, so a store that will not
                # take the write must not take the answer down with it (FR-110).
                try:
                    store_contribution(cache, key, contribution_of(analysis))
                except sqlite3.Error as exc:
                    _LOGGER.info("could not cache %s: %s", ref.session_id, exc)
    if not analyses:
        raise NoSessionsFound(
            f"every selected session used a model this rate table cannot price: "
            f"{', '.join(skipped)}. Run `ccaudit pricing refresh`, or add the model to "
            f"{resolve_pricing_path()} with its min_cacheable_tokens."
        )
    return Selected(
        analyses=analyses,
        excluded=len(getattr(args, "exclude", None) or ()),
        skipped=skipped,
        recalled=recalled,
    )


def marimo_command() -> list[str] | None:
    """How to launch marimo in a sandbox, or ``None`` if uv is not installed.

    ``uvx`` and ``uv tool run`` are the same thing spelled two ways, and which one exists is
    the only question here — but the two take *different arguments*, so getting the answer
    wrong does not fail, it runs something else. It did: the check was
    ``which("uvx").endswith("uvx")``, which is false for Windows' ``uvx.exe``, so the command
    became ``uvx tool run marimo`` — and uvx read "tool" as a package name and went and
    installed a PyPI project called ``tool``.

    So the test is on the executable's **name**, read with ``PureWindowsPath`` — which accepts
    both separator styles and strips the ``.exe`` — rather than with ``Path``, which on POSIX
    treats a backslash as an ordinary character and would not find the name at all.
    """
    uvx = shutil.which("uvx")
    if uvx is not None and PureWindowsPath(uvx).stem.lower() == "uvx":
        return [uvx, "marimo"]
    uv = shutil.which("uv")
    if uv is not None:
        return [uv, "tool", "run", "marimo"]
    # `which("uvx")` found something that is not uvx. Falling back to it as though it were
    # would run an unrelated program with marimo's arguments.
    return None


def _analyse_fresh(args: argparse.Namespace) -> list[SessionAnalysis]:
    """Analyse the selection without touching the cache.

    `explain` and `footprint` need the live `Pricing` object — the provenance line, the rates
    behind a formula — and a cached *conclusion* deliberately does not carry it: rates are
    configuration, not something the analysis concluded, and freezing them into a cache entry
    is how a figure ends up quoting a table that no longer prices it. Both commands are
    single-session and interactive, so recomputing costs a fraction of a second.
    """
    selected = _analyse_selection(args, cached=False)
    fresh = [item for item in selected.analyses if isinstance(item, SessionAnalysis)]
    if len(fresh) != len(selected.analyses):  # pragma: no cover - cached=False guarantees this
        raise AssertionError("cached=False must yield freshly-computed analyses")
    return fresh


@contextmanager
def _result_cache(*, enabled: bool = True) -> Iterator[sqlite3.Connection | None]:
    """The store, if it can be opened — and ``None`` if it cannot.

    A cache that fails to open must not stop an analysis: the figures do not come from it, and
    a read-only home directory or a database written by a newer build is a reason to be slow,
    not a reason to produce nothing (FR-110). Set ``CCAUDIT_NO_CACHE`` to skip it entirely,
    which is how the system tests prove the figures are identical without one.
    """
    if not enabled or os.environ.get(NO_CACHE_ENV):
        yield None
        return
    try:
        conn = connect()
    except (sqlite3.Error, SchemaVersionError, OSError) as exc:
        _LOGGER.info("running without the result cache: %s", exc)
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


def _run_analyse(args: argparse.Namespace) -> int:
    if getattr(args, "watch", False):
        return _run_watch(args)
    selected = _analyse_selection(args)
    payload = build_report_data(
        selected.analyses,
        redact=args.redact,
        sessions_excluded_count=selected.excluded,
        sessions_skipped=selected.skipped,
        group_by=args.group_by,
        sort_by=args.sort_by,
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
                analysis = analyse_transcript(
                    target,
                    pricing=pricing,
                    project_path=str(found.project_path) if found and found.project_path else None,
                    title=found.title if found else None,
                )
                store_result(connection, analysis, fingerprint)
                # And the cache the *analysis path* reads. Without this the worker filled the
                # normalised tables and warmed nothing: a later `ccaudit` run still recomputed
                # the session from its transcript, which defeats the point of doing the work in
                # advance (FR-088).
                store_contribution(
                    connection,
                    cache_key(
                        analysis.session_id,
                        fingerprint,
                        analysis.policy,
                        pricing.fingerprint,
                    ),
                    contribution_of(analysis),
                )
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
                selected = _analyse_selection(args)
                payload = build_report_data(
                    selected.analyses,
                    redact=args.redact,
                    sessions_excluded_count=selected.excluded,
                    sessions_skipped=selected.skipped,
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

    if getattr(args, "json", False):
        print(
            json.dumps(
                [
                    {
                        "session_id": ref.session_id,
                        "short_id": ref.short_id,
                        "title": ref.title,
                        "display_name": ref.display_name,
                        "project": str(ref.project_path or ref.project_dir),
                        "modified_at": ref.modified_at.isoformat(),
                        "record_count": ref.record_count,
                        "byte_size": ref.byte_size,
                        "in_progress": ref.in_progress,
                    }
                    for ref in refs
                ],
                indent=2,
            )
        )
        return EXIT_OK

    console = build_console()
    console.print(f"{len(refs)} session(s) available\n")
    # Name first, then the id fragment that selects it, then the full id — a listing is where
    # someone goes to *find* a session, and a wall of UUIDs is no help with that.
    for ref in refs:
        project = ref.project_path or ref.project_dir
        marker = "  (in progress)" if ref.in_progress else ""
        console.print(
            f"{ref.short_id}  {ref.modified_at:%Y-%m-%d %H:%M}  "
            f"{ref.record_count:>6,} records  {ref.byte_size / 1e6:>6.1f} MB  "
            f"{ref.title or '(unnamed)'}{marker}"
        )
        console.print(f"{'':10}{ref.session_id}  ·  {project}")
    return EXIT_OK


def _run_ui(args: argparse.Namespace) -> int:
    """Serve the exploring surface on loopback, and leave nothing behind (FR-072, FR-073).

    A command that happens to render in a browser, not a service: it blocks until stopped and
    releases the port on every exit path. The browser computes no figure of its own — it
    renders the same payload `--json` prints, which is what makes FR-074 structurally true.
    """
    refs = select_sessions(args)
    pricing = load_pricing()
    console = build_console()

    def provider(selection: Selection) -> dict[str, Any]:
        chosen = [ref for ref in refs if ref.session_id in selection.session_ids] or refs
        analyses = [
            analyse_transcript(
                ref.path,
                pricing=pricing,
                policy=args.policy,
                project_path=str(ref.project_path) if ref.project_path else None,
                provisional=ref.in_progress,
            )
            for ref in chosen
        ]
        return build_report_data(
            analyses,
            redact=selection.redact,
            sessions_excluded_count=len(getattr(args, "exclude", None) or ()),
            group_by=selection.group_by,
        )

    serve_ui(
        provider,
        refs,
        Selection(
            session_ids=tuple(ref.session_id for ref in refs),
            group_by=args.group_by,
            redact=args.redact,
        ),
        open_browser=args.open_browser,
        announce=console.print,
    )
    return EXIT_OK


def _run_footprint(args: argparse.Namespace) -> int:
    """Disclose the tool's own resident cost rather than asserting it is negligible (FR-056)."""
    analyses = _analyse_fresh(args)
    console = build_console()
    for analysis in analyses:
        for line in measure_footprint(analysis).lines():
            console.print(line)
    return EXIT_OK


def _run_report(args: argparse.Namespace) -> int:
    """Write the shareable report — one file, opens offline, no tooling required (FR-032)."""
    selected = _analyse_selection(args)
    payload = build_report_data(
        selected.analyses,
        redact=args.redact,
        sessions_excluded_count=selected.excluded,
        sessions_skipped=selected.skipped,
        group_by=args.group_by,
        sort_by=args.sort_by,
    )
    path = write_report(payload, args.out)
    console = build_console()
    console.print(f"Wrote {path}")
    console.print(
        "It is self-contained: it opens in any browser with no network and no tooling. "
        "Every figure in it is an API-equivalent cost estimate, not an amount charged."
    )
    if not args.redact:
        console.print(
            "It contains file paths from your sessions. Re-run with --redact before sharing "
            "it outside the team."
        )
    if args.open_report:
        webbrowser.open(path.resolve().as_uri())
    return EXIT_OK


def _run_notebook(args: argparse.Namespace) -> int:
    """Open a throwaway notebook, or write one to keep.

    The default is the same bargain as ``ccaudit ui``: one command, and nothing left behind
    when you stop it. The notebook is written to a temporary directory, marimo is launched over
    it, and the directory goes when the command exits — including on Ctrl-C, which is the exit
    that actually happens.

    ``--out`` is the other intent: give me the file, I will run it myself. Then nothing is
    launched and nothing is cleaned up, because the file is the deliverable.
    """
    console = build_console()
    if args.out is not None:
        path = write_notebook(args.out)
        console.print(f"Wrote {path}")
        console.print(
            f"Run it with:  uvx marimo edit --sandbox {path}\n"
            f"That installs marimo into a throwaway environment for that file alone — ccaudit "
            f"gains no dependency, and neither does your machine."
        )
        return EXIT_OK

    command = marimo_command()
    if command is None:
        raise UsageError(
            "the notebook needs `uvx` to run marimo in a sandbox, and neither uvx nor uv is on "
            "PATH. Install uv (https://docs.astral.sh/uv/), or run `ccaudit notebook --out "
            "notebook.py` and open the file with a marimo you already have."
        )

    # A temporary directory rather than a temporary file: marimo writes its own state beside
    # the notebook (`__marimo__/`), and cleaning up the notebook while leaving that behind
    # would be the kind of litter this command exists not to leave.
    with tempfile.TemporaryDirectory(prefix="ccaudit-notebook-") as workspace:
        path = write_notebook(Path(workspace) / DEFAULT_NOTEBOOK.name)
        console.print(
            "Opening a marimo notebook. It is temporary — it and everything marimo writes "
            "beside it are deleted when you stop this command."
        )
        console.print(
            "Every figure in it comes from ccaudit itself, so it cannot show a number the "
            "terminal would not. Press Ctrl-C here when you are done."
        )
        console.print(f"Keep a copy instead with:  ccaudit notebook --out {DEFAULT_NOTEBOOK}")
        try:
            completed = subprocess.run([*command, "edit", "--sandbox", str(path)], check=False)
        except KeyboardInterrupt:
            # Ctrl-C reaches marimo first and this process second. Nothing to report: the user
            # asked it to stop and it stopped. The directory goes on the way out of the block.
            console.print("Notebook closed. Nothing was left on disk.")
            return EXIT_INTERRUPTED
    console.print("Notebook closed. Nothing was left on disk.")
    return EXIT_OK if completed.returncode == 0 else EXIT_DATA_ERROR


def _run_explain(args: argparse.Namespace) -> int:
    analyses = _analyse_fresh(args)
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
    stale = pricing.staleness_note()
    if stale:
        print(f"\n{stale}")
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
