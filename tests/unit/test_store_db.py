"""Contract on the SQLite store — the schema constraints and the write boundary.

These tests are the durable fence on `ccaudit.store.db` and `ccaudit.store.schema.sql`
(constitution Principle V). Each one pins a named data-model invariant: a failure here means
a previously agreed contract was breached, not that the test needs updating.
"""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from ccaudit.store.db import (
    DB_FILENAME,
    SCHEMA_VERSION,
    connect,
    database_path,
    transaction,
)


@pytest.fixture
def conn(ccaudit_home: Path) -> Iterator[sqlite3.Connection]:
    """A store in an isolated CCAUDIT_HOME, created on demand exactly as in production."""
    connection = connect()
    yield connection
    connection.close()


def insert_session(conn: sqlite3.Connection, session_id: str = "s1") -> str:
    conn.execute(
        "INSERT INTO session (session_id, project_path, transcript_path, started_at, "
        "producing_version, is_complete) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, "/proj", "/proj/t.jsonl", "2026-08-11T10:00:00Z", "2.1.220", 1),
    )
    return session_id


def insert_turn(
    conn: sqlite3.Connection,
    turn_id: str = "t1",
    session_id: str = "s1",
    ordinal: int = 0,
    message_id: str = "msg_1",
    request_id: str = "req_1",
) -> str:
    conn.execute(
        "INSERT INTO turn (turn_id, session_id, ordinal, message_id, request_id, model, "
        "cache_ttl, is_sidechain) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (turn_id, session_id, ordinal, message_id, request_id, "claude-opus-5", "5m", 0),
    )
    return turn_id


def insert_attribution(
    conn: sqlite3.Connection,
    attribution_id: str = "a1",
    session_id: str = "s1",
    basis: str | None = "exact",
    confidence: str | None = "high",
    target_kind: str = "prompt",
    target_id: str | None = "p1",
    component: str | None = "overhead",
) -> None:
    conn.execute(
        "INSERT INTO attribution (attribution_id, session_id, turn_id, target_kind, target_id, "
        "component, cost_micros, basis, confidence, source_refs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attribution_id,
            session_id,
            None,
            target_kind,
            target_id,
            component,
            1234,
            basis,
            confidence,
            json.dumps(["rec:1"]),
        ),
    )


def _open_a_transaction(conn: sqlite3.Connection) -> None:
    """Stand-in for a callee that opens its own write boundary on a shared connection."""
    with transaction(conn):
        pass


class TestConnect:
    def test_state_directory_and_database_are_created_on_first_use(
        self, ccaudit_home: Path
    ) -> None:
        """No setup step and no first-run wizard: the store appears on demand (FR-050)."""
        assert not ccaudit_home.exists()
        connection = connect()
        try:
            assert (ccaudit_home / DB_FILENAME).is_file()
            assert database_path() == ccaudit_home / DB_FILENAME
        finally:
            connection.close()

    def test_schema_version_is_recorded(self, conn: sqlite3.Connection) -> None:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_connecting_twice_is_idempotent_and_keeps_data(self, ccaudit_home: Path) -> None:
        """Re-running must not recreate or wipe the store (Scripting Standards: idempotent)."""
        first = connect()
        try:
            with transaction(first):
                insert_session(first, "kept")
        finally:
            first.close()

        second = connect()
        try:
            assert second.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1
            assert second.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            second.close()

    def test_wal_journalling_is_on(self, conn: sqlite3.Connection) -> None:
        """A reader must never be blocked by the writer mid-analysis."""
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_foreign_keys_are_enforced(self, conn: sqlite3.Connection) -> None:
        """An orphaned child would drop out of a join and silently shrink a total."""
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_turn(conn, session_id="no-such-session")

    def test_every_data_model_entity_has_a_table(self, conn: sqlite3.Connection) -> None:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "session",
            "turn",
            "charge",
            "context_item",
            "injection",
            "residency_span",
            "cache_lane",
            "invalidation_event",
            "attribution",
            "analysis_result",
            "claim",
            "ingest_diagnostic",
        } <= names


class TestTurnDedup:
    def test_duplicate_message_and_request_id_is_rejected(self, conn: sqlite3.Connection) -> None:
        """Resume, compact, and fork repeat the same message; a second insert must fail (FR-021)."""
        with transaction(conn):
            insert_session(conn)
            insert_turn(conn, turn_id="t1")
        with pytest.raises(sqlite3.IntegrityError, match="turn.message_id, turn.request_id"):
            insert_turn(conn, turn_id="t2", message_id="msg_1", request_id="req_1")

    def test_distinct_request_ids_are_kept(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            insert_session(conn)
            insert_turn(conn, turn_id="t1", request_id="req_1")
            insert_turn(conn, turn_id="t2", ordinal=1, request_id="req_2")
        assert conn.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 2


class TestAnalysisResultUniqueness:
    def _insert(
        self, conn: sqlite3.Connection, result_id: str, policy: str = "proportional"
    ) -> None:
        conn.execute(
            "INSERT INTO analysis_result (result_id, session_id, fingerprint, "
            "covered_through_turn, policy, producing_version, tool_version, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (result_id, "s1", "fp-abc", 40, policy, "2.1.220", "0.0.0", "2026-08-11T11:00:00Z"),
        )

    def test_rerunning_over_unchanged_records_creates_no_second_entry(
        self, conn: sqlite3.Connection
    ) -> None:
        """Invariant F2: UNIQUE(session_id, fingerprint, policy) (FR-094)."""
        with transaction(conn):
            insert_session(conn)
            self._insert(conn, "r1")
        with pytest.raises(sqlite3.IntegrityError, match="analysis_result.session_id"):
            self._insert(conn, "r2")

    def test_results_are_policy_scoped(self, conn: sqlite3.Connection) -> None:
        """The same records under a different splitting policy are a different result."""
        with transaction(conn):
            insert_session(conn)
            self._insert(conn, "r1", policy="proportional")
            self._insert(conn, "r2", policy="first_touch")
        assert conn.execute("SELECT COUNT(*) FROM analysis_result").fetchone()[0] == 2


class TestTransaction:
    def test_an_exception_leaves_nothing_readable(self, conn: sqlite3.Connection) -> None:
        """Invariant K2: a partial computation is never readable as complete (FR-093)."""
        with pytest.raises(RuntimeError, match="computation blew up"), transaction(conn):
            insert_session(conn)
            insert_turn(conn)
            raise RuntimeError("computation blew up")
        assert conn.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM turn").fetchone()[0] == 0

    def test_a_constraint_violation_rolls_back_the_whole_write(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError), transaction(conn):
            insert_session(conn)
            insert_turn(conn, turn_id="t1")
            insert_turn(conn, turn_id="t2", message_id="msg_1", request_id="req_1")
        assert conn.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0

    def test_success_commits(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            insert_session(conn)
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1

    def test_nesting_raises_rather_than_faking_an_inner_commit(
        self, conn: sqlite3.Connection
    ) -> None:
        """SQLite has no nested transactions; an inner 'commit' would still be rolled back."""
        with transaction(conn):
            insert_session(conn)
            with pytest.raises(RuntimeError, match="no nested transactions"):
                _open_a_transaction(conn)
        assert conn.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 1


class TestAttributionProvenance:
    def test_basis_may_not_be_null(self, conn: sqlite3.Connection) -> None:
        """Every figure carries how it was derived — no nullable default (FR-014)."""
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL.*attribution.basis"):
            insert_attribution(conn, basis=None)

    def test_confidence_may_not_be_null(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL.*attribution.confidence"):
            insert_attribution(conn, confidence=None)

    def test_unknown_basis_is_rejected(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert_attribution(conn, basis="guessed")

    def test_output_is_never_charged_to_an_item(self, conn: sqlite3.Connection) -> None:
        """Invariant A2 (FR-005)."""
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert_attribution(conn, target_kind="item", target_id="i1", component="output")

    def test_the_remainder_has_neither_a_target_nor_a_component(
        self, conn: sqlite3.Connection
    ) -> None:
        """CHANGED INVARIANT — reviewed and accepted 2026-08-11.

        This test previously required a target on everything except the unattributed
        remainder. That was wrong in two ways, and both showed up the first time a real
        analysis was stored:

        1. **`prompt` legitimately has no target.** Output and overhead are charged to the
           exchange, never to a file — that is invariant A2 (FR-005), not an edge case. The old
           CHECK forced the store to invent a stand-in target id, which would have made a
           per-item query silently attribute conversation cost to something.
        2. **The remainder is not a cost component.** It was being stored as `overhead`, so a
           `GROUP BY component` folded "we could not attribute this" into "the conversation
           itself" — precisely the quietly-wrong number Principle X exists to prevent.

        The schema now pairs them: the remainder has no target *and* no component, and nothing
        else may omit either. The rule is narrower and matches what the model layer actually
        emits, rather than what the schema author assumed it would.
        """
        with transaction(conn):
            insert_session(conn)
            insert_attribution(
                conn, "a1", target_kind="unattributed", target_id=None, component=None
            )

        # A prompt-targeted attribution with no target is now accepted, because output and
        # overhead belong to the exchange.
        with transaction(conn):
            insert_attribution(conn, "a2", target_kind="prompt", target_id=None)

    def test_an_item_attribution_must_name_its_item(self, conn: sqlite3.Connection) -> None:
        """The relaxation above must not have opened the door for items too."""
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert_attribution(conn, "a3", target_kind="item", target_id=None, component="carry")

    def test_the_remainder_may_not_carry_a_component(self, conn: sqlite3.Connection) -> None:
        """Otherwise it hides inside a per-component breakdown."""
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert_attribution(
                conn, "a4", target_kind="unattributed", target_id=None, component="overhead"
            )

    def test_an_ordinary_attribution_must_carry_a_component(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert_attribution(conn, "a5", target_kind="prompt", target_id=None, component=None)


class TestClaim:
    def _claim(
        self,
        conn: sqlite3.Connection,
        state: str = "running",
        claimed_at: str = "2026-08-11T10:00:00Z",
        expires_at: str = "2026-08-11T10:05:00Z",
        pid: str = "111",
        host: str = "box-a",
        statement: str = "INSERT INTO claim",
    ) -> None:
        conn.execute(
            f"{statement} (session_id, fingerprint, state, claimed_at, expires_at, pid, host) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("s1", "fp-abc", state, claimed_at, expires_at, pid, host),
        )

    def test_one_claim_per_session_and_fingerprint(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            insert_session(conn)
            self._claim(conn)
        with pytest.raises(sqlite3.IntegrityError, match="claim.session_id"):
            self._claim(conn)

    def test_an_expired_claim_is_reclaimable_without_manual_cleanup(
        self, conn: sqlite3.Connection
    ) -> None:
        """Invariant K1: expiry is the crash-recovery mechanism (FR-092).

        The claim *logic* lands in `store/claims.py`; what the schema must guarantee is that a
        lapsed lease is visible as lapsed and can be taken over in place by another actor.
        """
        now = "2026-08-11T12:00:00Z"
        with transaction(conn):
            insert_session(conn)
            self._claim(conn, claimed_at="2026-08-11T10:00:00Z", expires_at="2026-08-11T10:05:00Z")

        with transaction(conn):
            taken = conn.execute(
                "UPDATE claim SET state = 'running', claimed_at = ?, expires_at = ?, "
                "pid = ?, host = ? WHERE session_id = ? AND fingerprint = ? AND expires_at <= ?",
                (now, "2026-08-11T12:05:00Z", "222", "box-b", "s1", "fp-abc", now),
            ).rowcount
        assert taken == 1

        row = conn.execute("SELECT pid, host, expires_at FROM claim").fetchone()
        assert (row["pid"], row["host"]) == ("222", "box-b")
        assert conn.execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 1

    def test_a_live_claim_is_not_stolen_by_the_same_statement(
        self, conn: sqlite3.Connection
    ) -> None:
        with transaction(conn):
            insert_session(conn)
            self._claim(conn, claimed_at="2026-08-11T11:59:00Z", expires_at="2026-08-11T12:04:00Z")
        with transaction(conn):
            taken = conn.execute(
                "UPDATE claim SET pid = ? WHERE session_id = ? AND expires_at <= ?",
                ("222", "s1", "2026-08-11T12:00:00Z"),
            ).rowcount
        assert taken == 0

    def test_a_lease_expiring_before_it_is_taken_is_rejected(
        self, conn: sqlite3.Connection
    ) -> None:
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            self._claim(conn, claimed_at="2026-08-11T10:05:00Z", expires_at="2026-08-11T10:00:00Z")

    def test_unknown_state_is_rejected(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            insert_session(conn)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            self._claim(conn, state="stale")
