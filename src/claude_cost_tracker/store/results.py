"""Persist an analysis, read it back, and say whether it is still current.

Two failure modes live here, and both would produce a confidently wrong answer rather than a
visible error:

**Serving a stale result as current.** A session that has advanced since it was analysed must
never have its old figures presented as current (FR-084, invariant F1). So a stored result
records the coverage fingerprint it covered, and :func:`check_freshness` compares that against
the session's *present* fingerprint. There is no code path that returns a stored result without
also saying whether it is current — ``current`` / ``stale`` / ``absent`` is the answer shape.

**A partially-written result read as complete.** A half-computed breakdown does not add up, and
a breakdown that does not add up is this project's definition of a show-stopper. So the whole
write — session, turns, items, injections, spans, attributions, diagnostics, and the result row
— happens inside one :func:`~claude_cost_tracker.store.db.transaction` (invariant K2).

**The store is a cache, not an archive.** Exactly one result is kept per ``(session, policy)``:
storing a newer fingerprint replaces the older row and its detail rows. Keeping superseded
generations would mean two answers for the same question sitting in the same table, where a
naive ``SUM(cost_micros) ... WHERE session_id = ?`` silently double-counts. Re-running over
*unchanged* records rewrites the same rows under the same deterministic ids, so it creates no
second entry (invariant F2, FR-094) and no drift.

**Ids are derived, never random.** ``result_id`` is ``session|policy|fingerprint`` and each
attribution id extends it. Two processes racing over the same records therefore write the same
rows, which is why the race is harmless: analysis is a pure function of the transcript.

**Nothing is translated on the way in.** What the model concluded is what is stored: a prompt
attribution keeps its absent target, the reconciled remainder is stored with **no component at
all** so no ``GROUP BY component`` can fold it into overhead, a span still resident at the end
of the records keeps its null ``last_turn``, and every ingest diagnostic is persisted under the
kind that produced it. A store that quietly reshapes its input is a store whose figures cannot
be traced back to the records that produced them (Principle VI).
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from claude_cost_tracker import __version__
from claude_cost_tracker.analyse import SessionAnalysis
from claude_cost_tracker.ingest.discover import Fingerprint
from claude_cost_tracker.model.reconcile import UNATTRIBUTED_LABEL
from claude_cost_tracker.store.db import transaction

__all__ = [
    "ABSENT",
    "CURRENT",
    "STALE",
    "Freshness",
    "ResultStoreError",
    "StoredAttribution",
    "StoredResult",
    "check_freshness",
    "iso_timestamp",
    "read_attributions",
    "read_result",
    "result_id_for",
    "store_result",
]

# The three answers to "is what we have any good?". `stale` and `absent` are different for the
# reader: stale still has figures and a stated coverage, absent has nothing to show.
CURRENT = "current"
STALE = "stale"
ABSENT = "absent"

# The reuse windows the schema records. `unknown` is a real state: there was a cache write but
# the record does not say which window it used, which caps the figure's confidence upstream.
_TTL_VALUES: frozenset[str] = frozenset({"5m", "1h", "unknown"})

_ID_SEPARATOR = "|"
_LIKE_ESCAPE = "\\"


class ResultStoreError(RuntimeError):
    """A stored analysis could not be written or read back consistently.

    Fatal by design (Principle I): every message names the session, and the record or value
    that broke the invariant, so the failure is triageable from the output alone.
    """


@dataclass(frozen=True)
class StoredAttribution:
    """One persisted conclusion, with the provenance that lets a skeptic check it."""

    attribution_id: str
    turn_id: str | None
    target_kind: str
    target_id: str | None
    # NULL for the unattributed remainder, which is money we could not explain rather than a
    # kind of cost. Anything grouping by component must treat it as its own line.
    component: str | None
    cost_micros: int
    basis: str
    confidence: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class StoredResult:
    """A result as it sits in the store: what it covered, and what it concluded.

    ``total_micros`` is not a stored column — it is the sum of the persisted attributions,
    which by invariant A1 *is* the session total. Deriving it means the figure and the
    breakdown cannot drift apart (Principle IX).
    """

    result_id: str
    session_id: str
    fingerprint: str
    covered_through_turn: int
    policy: str
    producing_version: str | None
    tool_version: str | None
    computed_at: str
    attributed_micros: int
    unattributed_micros: int
    attribution_count: int

    @property
    def total_micros(self) -> int:
        return self.attributed_micros + self.unattributed_micros

    def covers(self, fingerprint: Fingerprint) -> bool:
        return self.fingerprint == str(fingerprint)


@dataclass(frozen=True)
class Freshness:
    """Whether a stored result may be shown as current — and what to say when it may not.

    The answer always carries the fingerprint it was judged against, so a reader can reproduce
    the verdict without rerunning anything (Principle X).
    """

    status: str
    fingerprint: str
    result: StoredResult | None

    @property
    def is_current(self) -> bool:
        return self.status == CURRENT

    def coverage_note(self, current_through_turn: int | None = None) -> str | None:
        """How much of the session a stale result covers, in the reader's words.

        ``None`` when there is nothing to qualify: a current result needs no caveat, and an
        absent one has no coverage to state.
        """
        if self.result is None or self.status != STALE:
            return None
        covered = self.result.covered_through_turn
        note = f"covers turns 1-{covered}" if covered else "covers no turns"
        if current_through_turn is not None:
            note += f"; session is now at {current_through_turn}"
        return note


def iso_timestamp(moment: datetime) -> str:
    """The store's one timestamp format: ISO-8601 UTC to the second, so text sorts as time.

    Raises on a naive datetime rather than assuming local time — a lease that expires at an
    ambiguous instant is not a lease (Principle I).
    """
    if moment.tzinfo is None:
        raise ValueError(
            f"timestamp {moment!r} has no timezone; the store records UTC instants, and "
            f"guessing the zone would make an expiry mean different things on two machines"
        )
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def result_id_for(session_id: str, policy: str, fingerprint: Fingerprint | str) -> str:
    """The deterministic id of a result. Two racing analyses derive the same one."""
    return _ID_SEPARATOR.join((session_id, policy, str(fingerprint)))


def store_result(
    conn: sqlite3.Connection,
    analysis: SessionAnalysis,
    fingerprint: Fingerprint,
    *,
    project_path: str | None = None,
    tool_version: str = __version__,
    computed_at: datetime | None = None,
) -> StoredResult:
    """Write one analysis as a single all-or-nothing unit, and return what is now stored.

    Idempotent: re-running over unchanged records rewrites the same rows under the same ids
    and leaves exactly one result for this ``(session, policy)`` (invariant F2, FR-094).
    """
    moment = iso_timestamp(computed_at or datetime.now(UTC))
    result_id = result_id_for(analysis.session_id, analysis.policy, fingerprint)
    turn_ids = _turn_ids(analysis)

    with transaction(conn):
        _write_session(conn, analysis, project_path)
        _write_turns(conn, analysis, turn_ids)
        _write_items(conn, analysis)
        _write_injections(conn, analysis, turn_ids)
        _write_spans(conn, analysis)
        _write_attributions(conn, analysis, result_id, turn_ids)
        _write_diagnostics(conn, analysis)
        _write_result_row(conn, analysis, result_id, fingerprint, tool_version, moment)

    stored = read_result(conn, analysis.session_id, analysis.policy, fingerprint=fingerprint)
    if stored is None:  # pragma: no cover - the write above just committed this row
        raise ResultStoreError(
            f"session {analysis.session_id}: result {result_id} was written but could not be "
            f"read back; the store is inconsistent and its figures must not be used"
        )
    if stored.total_micros != analysis.total_micros:
        raise ResultStoreError(
            f"session {analysis.session_id}: stored breakdown sums to {stored.total_micros} "
            f"micro-dollars but the analysis totals {analysis.total_micros}. A stored "
            f"breakdown that does not add up is a show-stopper (invariant A1)"
        )
    return stored


def read_result(
    conn: sqlite3.Connection,
    session_id: str,
    policy: str,
    *,
    fingerprint: Fingerprint | None = None,
) -> StoredResult | None:
    """The stored result for a ``(session, policy)``, or ``None`` when there is none.

    Passing ``fingerprint`` asks for a *specific* generation: a stored result covering
    anything else is not returned, so no caller can receive figures for records it did not
    ask about. ``None`` is a normal outcome the caller branches on, not a failure.
    """
    row = conn.execute(
        "SELECT result_id, session_id, fingerprint, covered_through_turn, policy, "
        "producing_version, tool_version, computed_at FROM analysis_result "
        "WHERE session_id = ? AND policy = ?",
        (session_id, policy),
    ).fetchone()
    if row is None:
        return None
    if fingerprint is not None and row["fingerprint"] != str(fingerprint):
        return None

    totals = conn.execute(
        "SELECT COUNT(*) AS rows_stored, "
        "COALESCE(SUM(CASE WHEN target_kind = ? THEN 0 ELSE cost_micros END), 0) AS attributed, "
        "COALESCE(SUM(CASE WHEN target_kind = ? THEN cost_micros ELSE 0 END), 0) AS remainder "
        "FROM attribution WHERE session_id = ? AND attribution_id LIKE ? ESCAPE ?",
        (
            UNATTRIBUTED_LABEL,
            UNATTRIBUTED_LABEL,
            session_id,
            _prefix_pattern(row["result_id"]),
            _LIKE_ESCAPE,
        ),
    ).fetchone()

    return StoredResult(
        result_id=row["result_id"],
        session_id=row["session_id"],
        fingerprint=row["fingerprint"],
        covered_through_turn=row["covered_through_turn"],
        policy=row["policy"],
        producing_version=row["producing_version"],
        tool_version=row["tool_version"],
        computed_at=row["computed_at"],
        attributed_micros=int(totals["attributed"]),
        unattributed_micros=int(totals["remainder"]),
        attribution_count=int(totals["rows_stored"]),
    )


def read_attributions(conn: sqlite3.Connection, result: StoredResult) -> list[StoredAttribution]:
    """The stored breakdown behind a result, in the order it was computed."""
    rows = conn.execute(
        "SELECT attribution_id, turn_id, target_kind, target_id, component, cost_micros, "
        "basis, confidence, source_refs FROM attribution "
        "WHERE session_id = ? AND attribution_id LIKE ? ESCAPE ? ORDER BY attribution_id",
        (result.session_id, _prefix_pattern(result.result_id), _LIKE_ESCAPE),
    ).fetchall()
    return [
        StoredAttribution(
            attribution_id=row["attribution_id"],
            turn_id=row["turn_id"],
            target_kind=row["target_kind"],
            target_id=row["target_id"],
            component=row["component"],
            cost_micros=int(row["cost_micros"]),
            basis=row["basis"],
            confidence=row["confidence"],
            source_refs=tuple(json.loads(row["source_refs"])),
        )
        for row in rows
    ]


def check_freshness(
    conn: sqlite3.Connection,
    session_id: str,
    fingerprint: Fingerprint,
    policy: str,
) -> Freshness:
    """Judge a stored result against the session's *present* fingerprint (invariant F1).

    A result is current only while it covers the records that are there now. Anything else is
    ``stale`` and comes back labelled as such, with its coverage available — never served as
    current (FR-084).
    """
    stored = read_result(conn, session_id, policy)
    if stored is None:
        return Freshness(status=ABSENT, fingerprint=str(fingerprint), result=None)
    status = CURRENT if stored.covers(fingerprint) else STALE
    return Freshness(status=status, fingerprint=str(fingerprint), result=stored)


def _turn_ids(analysis: SessionAnalysis) -> list[str]:
    """The stored id of each analysed turn, indexed the way attributions index turns.

    The transcript record's uuid *is* the turn's identity — deriving a fresh id would make the
    same records store differently on two runs and break idempotency.
    """
    ids = []
    for index, turn in enumerate(analysis.timeline.turns):
        if not turn.uuid:
            raise ResultStoreError(
                f"session {analysis.session_id}: turn {index} (line {turn.line}) has no uuid, "
                f"so it has no stable identity to store attributions against"
            )
        ids.append(turn.uuid)
    return ids


def _write_session(
    conn: sqlite3.Connection, analysis: SessionAnalysis, project_path: str | None
) -> None:
    timestamps = sorted(turn.timestamp for turn in analysis.timeline.turns if turn.timestamp)
    conn.execute(
        "INSERT INTO session (session_id, project_path, transcript_path, started_at, ended_at, "
        "producing_version, is_complete) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (session_id) DO UPDATE SET project_path = excluded.project_path, "
        "transcript_path = excluded.transcript_path, started_at = excluded.started_at, "
        "ended_at = excluded.ended_at, producing_version = excluded.producing_version, "
        "is_complete = excluded.is_complete",
        (
            analysis.session_id,
            project_path,
            str(analysis.path),
            timestamps[0] if timestamps else None,
            timestamps[-1] if timestamps else None,
            _producing_version(analysis),
            0 if analysis.provisional else 1,
        ),
    )


def _write_turns(conn: sqlite3.Connection, analysis: SessionAnalysis, turn_ids: list[str]) -> None:
    for index, (turn, turn_id) in enumerate(zip(analysis.timeline.turns, turn_ids, strict=True)):
        try:
            conn.execute(
                "INSERT INTO turn (turn_id, session_id, ordinal, message_id, request_id, model, "
                "cache_ttl, is_sidechain) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (turn_id) DO UPDATE SET session_id = excluded.session_id, "
                "ordinal = excluded.ordinal, message_id = excluded.message_id, "
                "request_id = excluded.request_id, model = excluded.model, "
                "cache_ttl = excluded.cache_ttl, is_sidechain = excluded.is_sidechain",
                (
                    turn_id,
                    analysis.session_id,
                    index,
                    turn.message_id,
                    turn.request_id,
                    turn.model,
                    _cache_ttl(turn.usage.ttl, turn.usage.cache_creation_tokens),
                    1 if turn.is_sidechain else 0,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Overwhelmingly this is UNIQUE(message_id, request_id): the same exchange already
            # stored under a different record uuid, which means dedup did not fence a resumed
            # or forked transcript. Name the exchange rather than re-raising a bare constraint.
            raise ResultStoreError(
                f"session {analysis.session_id}: turn {turn_id} (ordinal {index}, message "
                f"{turn.message_id!r}, request {turn.request_id!r}) could not be stored: {exc}"
            ) from exc

    # Parents are linked in a second pass: `turn.parent_turn_id` is a foreign key onto `turn`,
    # and SQLite checks it immediately, so a child inserted before its parent would fail.
    known = set(turn_ids)
    for turn, turn_id in zip(analysis.timeline.turns, turn_ids, strict=True):
        parent = turn.parent_uuid if turn.parent_uuid in known else None
        if parent is not None and parent != turn_id:
            conn.execute("UPDATE turn SET parent_turn_id = ? WHERE turn_id = ?", (parent, turn_id))


def _write_items(conn: sqlite3.Connection, analysis: SessionAnalysis) -> None:
    for item in analysis.timeline.items.values():
        conn.execute(
            "INSERT INTO context_item (item_id, kind, identity, project_path, category, "
            "size_tokens) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (item_id) DO UPDATE SET kind = excluded.kind, "
            "identity = excluded.identity, project_path = excluded.project_path, "
            "category = excluded.category, size_tokens = excluded.size_tokens",
            (
                item.item_id,
                item.kind,
                item.identity,
                item.project_path,
                item.category,
                item.size_tokens,
            ),
        )


def _write_injections(
    conn: sqlite3.Connection, analysis: SessionAnalysis, turn_ids: list[str]
) -> None:
    for injection in analysis.timeline.injections:
        conn.execute(
            "INSERT INTO injection (injection_id, turn_id, item_id, cause, tool_use_id, "
            "size_tokens) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (injection_id) DO UPDATE SET turn_id = excluded.turn_id, "
            "item_id = excluded.item_id, cause = excluded.cause, "
            "tool_use_id = excluded.tool_use_id, size_tokens = excluded.size_tokens",
            (
                injection.injection_id,
                _turn_id_at(analysis, turn_ids, injection.turn_index, "injection"),
                injection.item_id,
                injection.cause,
                None,
                injection.size_tokens,
            ),
        )


def _write_spans(conn: sqlite3.Connection, analysis: SessionAnalysis) -> None:
    """Store residency spans exactly as the timeline concluded them.

    A span still resident when the records ran out keeps its null ``last_turn`` alongside
    ``end_reason='session_end'``: forcing a last turn onto it would invent an end the records
    do not show, and every such span belongs to an item that never left.
    """
    for span in analysis.timeline.spans:
        conn.execute(
            "INSERT INTO residency_span (span_id, injection_id, item_id, first_turn, last_turn, "
            "end_reason) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (span_id) DO UPDATE SET injection_id = excluded.injection_id, "
            "item_id = excluded.item_id, first_turn = excluded.first_turn, "
            "last_turn = excluded.last_turn, end_reason = excluded.end_reason",
            (
                # Span ids are only unique within a session (they are item plus turn index), so
                # the session namespaces them here — two sessions reading the same file on the
                # same turn are two spans, not one overwritten by the other.
                f"{analysis.session_id}{_ID_SEPARATOR}{span.span_id}",
                _opening_injection(analysis, span.item_id, span.first_turn),
                span.item_id,
                span.first_turn,
                span.last_turn,
                span.end_reason,
            ),
        )


def _write_attributions(
    conn: sqlite3.Connection,
    analysis: SessionAnalysis,
    result_id: str,
    turn_ids: list[str],
) -> None:
    """Replace this ``(session, policy)``'s breakdown with the one just computed.

    The delete is what keeps the table honest: without it, a re-analysis of a longer session
    would leave the previous generation's rows alongside the new ones, and a per-session sum
    would count the same money twice.
    """
    conn.execute(
        "DELETE FROM attribution WHERE session_id = ? AND attribution_id LIKE ? ESCAPE ?",
        (
            analysis.session_id,
            _prefix_pattern(f"{analysis.session_id}{_ID_SEPARATOR}{analysis.policy}"),
            _LIKE_ESCAPE,
        ),
    )

    rows: list[tuple[str, str | None, str, str | None, str | None, int, str, str, str]] = []
    for index, attribution in enumerate(analysis.attribution.attributions):
        turn_id = _turn_id_at(analysis, turn_ids, attribution.turn_index, "attribution")
        rows.append(
            (
                f"{result_id}{_ID_SEPARATOR}{index:06d}",
                turn_id,
                attribution.target_kind,
                # A prompt attribution names no target on purpose: output and overhead belong
                # to the exchange, which is not an entity with an id (invariant A2).
                attribution.target_id,
                attribution.component,
                attribution.cost_micros,
                attribution.basis,
                attribution.confidence,
                json.dumps(list(attribution.source_refs)),
            )
        )

    remainder = analysis.reconciliation.unattributed_micros
    if remainder:
        # The remainder is a first-class row so that SUM(attribution) equals the session total
        # exactly (invariant A1), and it carries **no component**: it is money we could not
        # explain, not a fifth kind of cost. A NULL keeps it out of every per-component
        # aggregate instead of quietly inflating one of them (FR-013).
        rows.append(
            (
                f"{result_id}{_ID_SEPARATOR}{len(rows):06d}",
                None,
                UNATTRIBUTED_LABEL,
                None,
                None,
                remainder,
                "exact",
                "high",
                json.dumps([f"reconciliation of session {analysis.session_id}"]),
            )
        )

    conn.executemany(
        "INSERT INTO attribution (attribution_id, session_id, turn_id, target_kind, target_id, "
        "component, cost_micros, basis, confidence, source_refs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(row[0], analysis.session_id, *row[1:]) for row in rows],
    )


def _write_diagnostics(conn: sqlite3.Connection, analysis: SessionAnalysis) -> None:
    """Persist every record that could not be used, under the kind that produced it.

    Kinds are minted where the problem is detected, so this stores whatever ingest reported —
    a diagnostic the store did not anticipate is exactly the one worth keeping (FR-026,
    FR-027). Rewritten wholesale each time so a re-analysis cannot leave a stale count behind.
    """
    conn.execute("DELETE FROM ingest_diagnostic WHERE session_id = ?", (analysis.session_id,))
    for diagnostic in analysis.diagnostics.values():
        conn.execute(
            "INSERT INTO ingest_diagnostic (session_id, kind, count, sample) VALUES (?, ?, ?, ?)",
            (
                analysis.session_id,
                diagnostic.kind,
                diagnostic.count,
                "\n".join(diagnostic.samples) or None,
            ),
        )


def _write_result_row(
    conn: sqlite3.Connection,
    analysis: SessionAnalysis,
    result_id: str,
    fingerprint: Fingerprint,
    tool_version: str,
    computed_at: str,
) -> None:
    # One result per (session, policy): a superseded generation is replaced, never accumulated
    # alongside the current one (see module docstring).
    conn.execute(
        "DELETE FROM analysis_result WHERE session_id = ? AND policy = ? AND result_id != ?",
        (analysis.session_id, analysis.policy, result_id),
    )
    conn.execute(
        "INSERT INTO analysis_result (result_id, session_id, fingerprint, covered_through_turn, "
        "policy, producing_version, tool_version, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (session_id, fingerprint, policy) DO UPDATE SET "
        "covered_through_turn = excluded.covered_through_turn, "
        "producing_version = excluded.producing_version, "
        "tool_version = excluded.tool_version, computed_at = excluded.computed_at",
        (
            result_id,
            analysis.session_id,
            str(fingerprint),
            len(analysis.timeline.turns),
            analysis.policy,
            _producing_version(analysis),
            tool_version,
            computed_at,
        ),
    )


def _producing_version(analysis: SessionAnalysis) -> str | None:
    """The Claude Code version(s) that produced these records (FR-028).

    A session that spans versions keeps all of them, comma-separated, so a comparison across
    the boundary can say that it crossed one instead of silently averaging over it.
    """
    return _join_versions(analysis.parsed.producing_versions)


def _join_versions(versions: Iterable[str]) -> str | None:
    ordered = sorted(version for version in versions if version)
    return ",".join(ordered) if ordered else None


def _cache_ttl(ttl: str | None, cache_creation_tokens: int) -> str | None:
    """The reuse window as the schema records it. NULL only when there was no write at all."""
    if not cache_creation_tokens:
        return None
    if ttl in _TTL_VALUES:
        return ttl
    return "unknown"


def _turn_id_at(analysis: SessionAnalysis, turn_ids: list[str], turn_index: int, what: str) -> str:
    if not 0 <= turn_index < len(turn_ids):
        raise ResultStoreError(
            f"session {analysis.session_id}: an {what} references turn index {turn_index}, but "
            f"the analysis covers {len(turn_ids)} turn(s). The breakdown does not describe the "
            f"records it was computed from"
        )
    return turn_ids[turn_index]


def _opening_injection(analysis: SessionAnalysis, item_id: str, first_turn: int) -> str:
    """The injection that opened a residency span — its required, non-null parent row.

    A span exists only because something was injected, so a span with no injection is a broken
    invariant upstream, not a row to store with a guessed parent.
    """
    for injection in analysis.timeline.injections:
        if injection.item_id == item_id and injection.turn_index == first_turn:
            return injection.injection_id
    raise ResultStoreError(
        f"session {analysis.session_id}: residency span for item {item_id!r} starting at turn "
        f"{first_turn} has no injection at that turn, so nothing explains why the item became "
        f"resident"
    )


def _prefix_pattern(prefix: str) -> str:
    """A LIKE pattern matching ids under ``prefix``, with LIKE's wildcards escaped.

    ``_`` and ``%`` are legal in policy names and session ids and are wildcards in LIKE, so an
    unescaped prefix could match a *different* policy's rows and delete or total the wrong
    money.
    """
    escaped = (
        prefix.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"{escaped}{_ID_SEPARATOR}%"
