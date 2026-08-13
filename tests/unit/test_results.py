"""Contract on result persistence — freshness, idempotency, and all-or-nothing writes.

These tests fence `claude_cost_tracker.store.results` (constitution Principle V). Each pins a named
invariant: F1 (never serve stale as current), F2 (re-running creates no second entry), K2 (a
partial write is never readable as complete), and A1 (what comes back out adds up). A failure
here is a breached contract, not a test to update.
"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_cost_tracker.analyse import SessionAnalysis, analyse_transcript
from claude_cost_tracker.ingest.discover import Fingerprint, fingerprint_transcript
from claude_cost_tracker.ingest.records import IngestDiagnostic
from claude_cost_tracker.store.db import connect
from claude_cost_tracker.store.results import (
    ABSENT,
    CURRENT,
    STALE,
    StoredAttribution,
    check_freshness,
    iso_timestamp,
    read_attributions,
    read_result,
    store_result,
)
from tests.fixtures.builder import TranscriptBuilder, simple_session

COMPUTED_AT = datetime(2026, 8, 11, 11, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(ccost_home: Path) -> Iterator[sqlite3.Connection]:
    connection = connect()
    yield connection
    connection.close()


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return simple_session().write(tmp_path / "session.jsonl")


@pytest.fixture
def analysis(transcript: Path) -> SessionAnalysis:
    return analyse_transcript(transcript)


@pytest.fixture
def fingerprint(transcript: Path) -> Fingerprint:
    return fingerprint_transcript(transcript)


def advanced(fingerprint: Fingerprint) -> Fingerprint:
    """The same session after two more records were appended — a moved fingerprint."""
    return Fingerprint(
        record_count=fingerprint.record_count + 2,
        last_record_uuid="asst-9999",
        byte_size=fingerprint.byte_size + 512,
    )


def figures(
    rows: list[StoredAttribution],
) -> list[tuple[str, str | None, str | None, int, str]]:
    """The part of a stored breakdown that must be identical between two runs."""
    return [
        (row.target_kind, row.target_id, row.component, row.cost_micros, row.basis) for row in rows
    ]


class TestStoreAndReadBack:
    def test_a_stored_result_reads_back_with_its_coverage_and_provenance(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        stored = store_result(conn, analysis, fingerprint, computed_at=COMPUTED_AT)

        assert stored.session_id == analysis.session_id
        assert stored.fingerprint == str(fingerprint)
        assert stored.policy == analysis.policy
        assert stored.covered_through_turn == len(analysis.timeline.turns)
        assert stored.computed_at == "2026-08-11T11:00:00Z"
        # Version-spanning comparisons must be identifiable (FR-028).
        assert stored.producing_version == "2.1.220"
        assert stored.tool_version

        assert read_result(conn, analysis.session_id, analysis.policy) == stored

    def test_the_stored_breakdown_adds_up_to_the_session_total(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """Invariant A1: what comes out of the store still reconciles, exactly."""
        stored = store_result(conn, analysis, fingerprint)

        rows = read_attributions(conn, stored)
        assert sum(row.cost_micros for row in rows) == analysis.total_micros
        assert stored.attributed_micros + stored.unattributed_micros == analysis.total_micros
        assert stored.unattributed_micros == analysis.reconciliation.unattributed_micros

    def test_the_unattributed_remainder_is_stored_as_its_own_visible_row(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """The remainder is never absorbed into the nearest bucket (FR-013)."""
        stored = store_result(conn, analysis, fingerprint)

        remainders = [
            row for row in read_attributions(conn, stored) if row.target_kind == "unattributed"
        ]
        assert len(remainders) == 1
        assert remainders[0].cost_micros == analysis.reconciliation.unattributed_micros
        assert remainders[0].target_id is None
        # Not a fifth kind of cost: money we could not explain.
        assert remainders[0].component is None

    def test_the_remainder_cannot_leak_into_a_per_component_aggregate(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """A `GROUP BY component` that absorbed the remainder would report a wrong figure."""
        store_result(conn, analysis, fingerprint)

        by_component = {
            row["component"]: row["total"]
            for row in conn.execute(
                "SELECT component, SUM(cost_micros) AS total FROM attribution "
                "WHERE session_id = ? GROUP BY component",
                (analysis.session_id,),
            )
        }
        remainder = analysis.reconciliation.unattributed_micros
        assert remainder > 0
        assert by_component[None] == remainder
        assert sum(total for component, total in by_component.items() if component is not None) == (
            analysis.reconciliation.attributed_micros
        )

    def test_a_prompt_attribution_names_no_target(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """Output and overhead belong to the exchange, not to any item (invariant A2)."""
        stored = store_result(conn, analysis, fingerprint)

        prompts = [row for row in read_attributions(conn, stored) if row.target_kind == "prompt"]
        assert prompts
        assert all(row.target_id is None for row in prompts)
        assert {row.component for row in prompts} <= {"output", "overhead"}
        # The exchange it belongs to is still identified — by the turn, not by a stand-in target.
        assert all(row.turn_id for row in prompts)

    def test_context_items_injections_and_spans_are_stored(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        store_result(conn, analysis, fingerprint)

        assert analysis.timeline.items
        assert conn.execute("SELECT COUNT(*) FROM context_item").fetchone()[0] == len(
            analysis.timeline.items
        )
        assert conn.execute("SELECT COUNT(*) FROM injection").fetchone()[0] == len(
            analysis.timeline.injections
        )
        assert conn.execute("SELECT COUNT(*) FROM residency_span").fetchone()[0] == len(
            analysis.timeline.spans
        )

    def test_a_span_still_resident_at_the_end_round_trips_with_no_last_turn(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """Still resident when the records ran out — an invented last turn would be a lie."""
        open_spans = [span for span in analysis.timeline.spans if span.last_turn is None]
        assert open_spans

        store_result(conn, analysis, fingerprint)

        rows = conn.execute(
            "SELECT last_turn, end_reason FROM residency_span WHERE last_turn IS NULL"
        ).fetchall()
        assert len(rows) == len(open_spans)
        assert {row["end_reason"] for row in rows} == {"session_end"}

    def test_an_agent_listing_delta_injection_is_stored_under_its_own_cause(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Stored as what it is — not aliased onto a neighbouring cause."""
        builder = simple_session()
        builder.add_attachment("agent_listing_delta", {"addedNames": ["explore"], "addedLines": 20})
        builder.add_turn(input_tokens=10, cache_read=2200, output_tokens=30)
        path = builder.write(tmp_path / "agents.jsonl")
        analysed = analyse_transcript(path)

        store_result(conn, analysed, fingerprint_transcript(path))

        causes = {row["cause"] for row in conn.execute("SELECT cause FROM injection")}
        assert "agent_listing_delta" in causes

    def test_reading_a_session_that_was_never_analysed_is_a_normal_empty_answer(
        self, conn: sqlite3.Connection
    ) -> None:
        assert read_result(conn, "never-seen", "proportional") is None


class TestFreshness:
    def test_a_result_covering_the_present_records_is_current(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        store_result(conn, analysis, fingerprint)

        freshness = check_freshness(conn, analysis.session_id, fingerprint, analysis.policy)
        assert freshness.status == CURRENT
        assert freshness.is_current
        assert freshness.coverage_note() is None

    def test_a_result_whose_fingerprint_no_longer_matches_is_stale_never_current(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """Invariant F1 / SC-030: a session that advanced must not be served as current."""
        store_result(conn, analysis, fingerprint)

        freshness = check_freshness(
            conn, analysis.session_id, advanced(fingerprint), analysis.policy
        )
        assert freshness.status == STALE
        assert not freshness.is_current
        assert freshness.result is not None
        assert freshness.result.covered_through_turn == len(analysis.timeline.turns)

    def test_a_stale_result_can_say_how_much_of_the_session_it_covers(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        store_result(conn, analysis, fingerprint)

        freshness = check_freshness(
            conn, analysis.session_id, advanced(fingerprint), analysis.policy
        )
        assert freshness.coverage_note(62) == "covers turns 1-2; session is now at 62"

    def test_asking_for_a_specific_fingerprint_never_returns_a_different_generation(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        store_result(conn, analysis, fingerprint)

        assert (
            read_result(
                conn, analysis.session_id, analysis.policy, fingerprint=advanced(fingerprint)
            )
            is None
        )
        assert (
            read_result(conn, analysis.session_id, analysis.policy, fingerprint=fingerprint)
            is not None
        )

    def test_no_stored_result_is_absent_not_stale(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        freshness = check_freshness(conn, analysis.session_id, fingerprint, analysis.policy)
        assert freshness.status == ABSENT
        assert freshness.result is None
        assert freshness.coverage_note(10) is None

    def test_freshness_is_policy_scoped(
        self, conn: sqlite3.Connection, transcript: Path, fingerprint: Fingerprint
    ) -> None:
        proportional = analyse_transcript(transcript, policy="proportional")
        store_result(conn, proportional, fingerprint)

        assert check_freshness(conn, proportional.session_id, fingerprint, "exclusive").status == (
            ABSENT
        )


class TestIdempotency:
    def test_rerunning_over_unchanged_records_creates_no_duplicate_row(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """Invariant F2 / SC-013, FR-094."""
        first = store_result(conn, analysis, fingerprint, computed_at=COMPUTED_AT)
        second = store_result(conn, analysis, fingerprint, computed_at=COMPUTED_AT)

        assert conn.execute("SELECT COUNT(*) FROM analysis_result").fetchone()[0] == 1
        assert first == second
        assert figures(read_attributions(conn, first)) == figures(read_attributions(conn, second))

    def test_two_simultaneous_analyses_leave_one_result_with_identical_figures(
        self, ccost_home: Path, transcript: Path, fingerprint: Fingerprint
    ) -> None:
        """SC-033. The race is simulated deterministically: two independent analyses of the
        same records, stored over two connections, interleaved by hand. Real threads would
        make the test a timing experiment, and timing is not what is under test — the claim
        that a race cannot change the answer is (analysis is a pure function).
        """
        left, right = connect(), connect()
        try:
            first = analyse_transcript(transcript)
            second = analyse_transcript(transcript)

            stored_left = store_result(left, first, fingerprint, computed_at=COMPUTED_AT)
            stored_right = store_result(right, second, fingerprint, computed_at=COMPUTED_AT)

            assert left.execute("SELECT COUNT(*) FROM analysis_result").fetchone()[0] == 1
            assert stored_left == stored_right
            assert figures(read_attributions(left, stored_left)) == figures(
                read_attributions(right, stored_right)
            )
        finally:
            left.close()
            right.close()

    def test_a_newer_fingerprint_replaces_the_generation_it_supersedes(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """Two generations in one table would let a per-session sum count money twice."""
        store_result(conn, analysis, fingerprint)
        moved = advanced(fingerprint)
        stored = store_result(conn, analysis, moved)

        assert conn.execute("SELECT COUNT(*) FROM analysis_result").fetchone()[0] == 1
        assert stored.fingerprint == str(moved)
        assert (
            conn.execute(
                "SELECT COALESCE(SUM(cost_micros), 0) FROM attribution WHERE session_id = ?",
                (analysis.session_id,),
            ).fetchone()[0]
            == analysis.total_micros
        )

    def test_the_same_session_under_two_policies_stores_two_retrievable_rows(
        self, conn: sqlite3.Connection, transcript: Path, fingerprint: Fingerprint
    ) -> None:
        proportional = analyse_transcript(transcript, policy="proportional")
        exclusive = analyse_transcript(transcript, policy="exclusive")

        store_result(conn, proportional, fingerprint)
        store_result(conn, exclusive, fingerprint)

        assert conn.execute("SELECT COUNT(*) FROM analysis_result").fetchone()[0] == 2
        for analysed in (proportional, exclusive):
            stored = read_result(conn, analysed.session_id, analysed.policy)
            assert stored is not None
            assert stored.policy == analysed.policy
            assert stored.total_micros == analysed.total_micros
            assert read_attributions(conn, stored)


class TestPartialWrites:
    def test_an_interrupted_write_leaves_nothing_readable(
        self,
        conn: sqlite3.Connection,
        analysis: SessionAnalysis,
        fingerprint: Fingerprint,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invariant K2 (FR-093): the result is absent, not half-present."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("computation blew up mid-write")

        monkeypatch.setattr("claude_cost_tracker.store.results._write_result_row", explode)

        with pytest.raises(RuntimeError, match="blew up"):
            store_result(conn, analysis, fingerprint)

        assert read_result(conn, analysis.session_id, analysis.policy) is None
        assert (
            check_freshness(conn, analysis.session_id, fingerprint, analysis.policy).status
            == ABSENT
        )
        for table in ("session", "turn", "attribution", "context_item", "injection"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    def test_a_half_written_result_never_replaces_a_good_one(
        self,
        conn: sqlite3.Connection,
        analysis: SessionAnalysis,
        fingerprint: Fingerprint,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = store_result(conn, analysis, fingerprint, computed_at=COMPUTED_AT)

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("computation blew up mid-write")

        monkeypatch.setattr("claude_cost_tracker.store.results._write_result_row", explode)
        with pytest.raises(RuntimeError, match="blew up"):
            store_result(conn, analysis, advanced(fingerprint))

        assert read_result(conn, analysis.session_id, analysis.policy) == good


class TestDiagnostics:
    def test_unusable_records_are_counted_in_the_store_not_dropped(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """FR-026/FR-027: a record that could not be used is named, never silently skipped."""
        builder = simple_session()
        builder.add_malformed_line()
        path = builder.write(tmp_path / "broken.jsonl")
        analysed = analyse_transcript(path)

        store_result(conn, analysed, fingerprint_transcript(path))

        row = conn.execute(
            "SELECT kind, count, sample FROM ingest_diagnostic WHERE session_id = ?",
            (analysed.session_id,),
        ).fetchone()
        # Stored under the kind that produced it, not folded into a coarser bucket.
        assert row["kind"] == "unparseable_json"
        assert row["count"] == 1
        assert "broken.jsonl" in row["sample"]

    def test_dedup_diagnostics_are_persisted_too(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A duplicated exchange is a record that could not be used, and is kept as one."""
        builder = simple_session()
        builder.add_turn(
            input_tokens=100,
            cache_creation_5m=2000,
            output_tokens=50,
            message_id="msg_0002",
            request_id="req_0002",
        )
        path = builder.write(tmp_path / "duplicated.jsonl")
        analysed = analyse_transcript(path)
        assert "duplicate_turn" in analysed.diagnostics

        store_result(conn, analysed, fingerprint_transcript(path))

        stored = {
            row["kind"]: row["count"]
            for row in conn.execute(
                "SELECT kind, count FROM ingest_diagnostic WHERE session_id = ?",
                (analysed.session_id,),
            )
        }
        assert stored["duplicate_turn"] == analysed.diagnostics["duplicate_turn"].count

    def test_a_diagnostic_kind_the_store_has_never_seen_is_stored_not_rejected(
        self, conn: sqlite3.Connection, analysis: SessionAnalysis, fingerprint: Fingerprint
    ) -> None:
        """A new kind of bad record must never fail the write — recording it is the point."""
        analysis.parsed.diagnostics["invented_kind"] = IngestDiagnostic(
            kind="invented_kind", count=3, samples=["line 9: something new"]
        )

        store_result(conn, analysis, fingerprint)

        row = conn.execute(
            "SELECT count, sample FROM ingest_diagnostic WHERE session_id = ? AND kind = ?",
            (analysis.session_id, "invented_kind"),
        ).fetchone()
        assert row["count"] == 3
        assert row["sample"] == "line 9: something new"


class TestTimestamps:
    def test_a_naive_timestamp_is_rejected(self) -> None:
        """An instant with no zone means different things on two machines."""
        with pytest.raises(ValueError, match="no timezone"):
            iso_timestamp(COMPUTED_AT.replace(tzinfo=None))

    def test_timestamps_are_utc_and_sort_lexically(self) -> None:
        earlier = iso_timestamp(datetime(2026, 8, 11, 9, 59, 59, tzinfo=UTC))
        later = iso_timestamp(datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC))
        assert earlier < later
        assert later == "2026-08-11T10:00:00Z"


class TestVersionSpanning:
    def test_a_session_spanning_versions_records_both(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """FR-028: a comparison that crosses a version boundary must be able to say so."""
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=100, cache_creation_5m=2000, output_tokens=50)
        builder.add_turn(input_tokens=10, cache_read=2100, output_tokens=20, version="2.1.221")
        path = builder.write(tmp_path / "spanning.jsonl")
        analysed = analyse_transcript(path)

        stored = store_result(conn, analysed, fingerprint_transcript(path))
        assert stored.producing_version == "2.1.220,2.1.221"
