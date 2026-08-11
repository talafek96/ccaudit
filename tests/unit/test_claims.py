"""Contract on claims — expiry as crash recovery, and a wait that always ends.

These tests fence `ccaudit.store.claims` (constitution Principle V) against invariants K1 (an
expired claim is reclaimable by anyone, with no manual cleanup) and K3 (a reader waits a
bounded interval, then computes the result itself).

**No threads, no real sleeping.** Two "racing" processes are simulated by two connections
taking the claim in turn, and the bounded wait runs its real loop over an injected clock that
a fake `sleep` advances. A test that used real concurrency would be measuring the scheduler;
what is under test is the statement and the loop.
"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ccaudit.ingest.discover import Fingerprint
from ccaudit.store.claims import (
    DONE,
    Claim,
    ClaimError,
    current_claim,
    mark_done,
    release_claim,
    take_claim,
    wait_for_claim,
)
from ccaudit.store.db import connect

SESSION = "11111111-2222-3333-4444-555555555555"
FINGERPRINT = Fingerprint(record_count=7, last_record_uuid="asst-0007", byte_size=2329)
NOON = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)
LEASE = timedelta(minutes=5)


class FakeClock:
    """A clock that only moves when something sleeps — the loop's own time, made observable."""

    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    def frozen_sleep(self, seconds: float) -> None:
        """A broken sleep: it records the call but never advances time."""
        self.slept.append(seconds)


@pytest.fixture
def conn(ccaudit_home: Path) -> Iterator[sqlite3.Connection]:
    connection = connect()
    yield connection
    connection.close()


def held(claim: Claim | None) -> tuple[str, str]:
    assert claim is not None
    return claim.pid, claim.host


class TestTakingAClaim:
    def test_a_claim_is_taken_and_readable_by_anything_else_asking(
        self, conn: sqlite3.Connection
    ) -> None:
        """FR-090: who holds it, since when, and whether it is still live."""
        taken = take_claim(
            conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111", host="box-a"
        )

        assert taken is not None
        assert taken.state == "running"
        assert taken.claimed_at == "2026-08-11T12:00:00Z"
        assert taken.expires_at == "2026-08-11T12:05:00Z"
        assert taken.holder == "pid 111 on box-a"
        assert taken.is_live(NOON)
        assert current_claim(conn, SESSION, FINGERPRINT) == taken
        assert "being analysed by pid 111 on box-a" in taken.describe(NOON)

    def test_two_racing_processes_do_not_both_believe_they_hold_it(
        self, ccaudit_home: Path
    ) -> None:
        """The upsert applies once; the loser is told `None` and can act on it."""
        first, second = connect(), connect()
        try:
            winner = take_claim(
                first, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111", host="box-a"
            )
            loser = take_claim(
                second,
                SESSION,
                FINGERPRINT,
                lease=LEASE,
                now=NOON + timedelta(seconds=1),
                pid="222",
                host="box-b",
            )

            assert winner is not None
            assert loser is None
            assert held(current_claim(second, SESSION, FINGERPRINT)) == ("111", "box-a")
            assert second.execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 1
        finally:
            first.close()
            second.close()

    def test_a_claim_on_a_different_fingerprint_is_a_different_claim(
        self, conn: sqlite3.Connection
    ) -> None:
        """Records that have moved on are a different question, not a contended one."""
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111", host="box-a")
        moved = Fingerprint(record_count=9, last_record_uuid="asst-0009", byte_size=3000)

        other = take_claim(conn, SESSION, moved, lease=LEASE, now=NOON, pid="222", host="box-b")
        assert other is not None
        assert conn.execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 2

    def test_a_lease_that_would_expire_when_taken_is_refused(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(ClaimError, match="positive lease"):
            take_claim(conn, SESSION, FINGERPRINT, lease=timedelta(0), now=NOON)


class TestExpiryIsCrashRecovery:
    def test_an_expired_claim_is_reclaimed_automatically_with_no_manual_step(
        self, conn: sqlite3.Connection
    ) -> None:
        """Invariant K1 / SC-034: a worker that died holding a claim blocks nothing."""
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111", host="box-a")

        after_expiry = NOON + LEASE + timedelta(seconds=1)
        reclaimed = take_claim(
            conn, SESSION, FINGERPRINT, lease=LEASE, now=after_expiry, pid="222", host="box-b"
        )

        assert reclaimed is not None
        assert held(reclaimed) == ("222", "box-b")
        assert reclaimed.expires_at == "2026-08-11T12:10:01Z"
        # Reclaimed in place: the lapsed lease is replaced, never accumulated alongside.
        assert conn.execute("SELECT COUNT(*) FROM claim").fetchone()[0] == 1

    def test_an_expired_claim_reports_itself_as_not_live(self, conn: sqlite3.Connection) -> None:
        claim = take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111")
        assert claim is not None

        assert not claim.is_live(NOON + LEASE + timedelta(seconds=1))
        assert "expired at" in claim.describe(NOON + LEASE + timedelta(seconds=1))

    def test_a_finished_claim_does_not_make_the_next_actor_wait_out_the_lease(
        self, conn: sqlite3.Connection
    ) -> None:
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111", host="box-a")
        mark_done(conn, SESSION, FINGERPRINT)

        finished = current_claim(conn, SESSION, FINGERPRINT)
        assert finished is not None
        assert finished.state == DONE
        assert not finished.is_live(NOON)

        next_actor = take_claim(
            conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="222", host="box-b"
        )
        assert held(next_actor) == ("222", "box-b")

    def test_marking_a_claim_nobody_holds_raises_rather_than_passing_silently(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(ClaimError, match="no claim on fingerprint"):
            mark_done(conn, SESSION, FINGERPRINT)

    def test_releasing_is_idempotent(self, conn: sqlite3.Connection) -> None:
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111")

        assert release_claim(conn, SESSION, FINGERPRINT) is True
        assert release_claim(conn, SESSION, FINGERPRINT) is False
        assert current_claim(conn, SESSION, FINGERPRINT) is None


class TestBoundedWait:
    def test_a_wait_returns_within_its_bound_when_the_claim_never_clears(
        self, conn: sqlite3.Connection
    ) -> None:
        """Invariant K3 (FR-091): the reader stops waiting and computes it itself."""
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111", host="box-a")
        clock = FakeClock(NOON)

        still_held = wait_for_claim(
            conn,
            SESSION,
            FINGERPRINT,
            timeout=timedelta(seconds=2),
            poll_interval=timedelta(milliseconds=500),
            clock=clock,
            sleep=clock.sleep,
        )

        assert still_held is not None
        assert held(still_held) == ("111", "box-a")
        # Bounded: it waited exactly the stated interval, no more.
        assert sum(clock.slept) == pytest.approx(2.0)
        assert clock.now == NOON + timedelta(seconds=2)

    def test_a_wait_ends_as_soon_as_the_claim_is_released(self, conn: sqlite3.Connection) -> None:
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111")
        clock = FakeClock(NOON)

        def release_after_one_poll(seconds: float) -> None:
            clock.sleep(seconds)
            release_claim(conn, SESSION, FINGERPRINT)

        cleared = wait_for_claim(
            conn,
            SESSION,
            FINGERPRINT,
            timeout=timedelta(seconds=10),
            poll_interval=timedelta(seconds=1),
            clock=clock,
            sleep=release_after_one_poll,
        )

        assert cleared is None
        assert clock.slept == [1.0]

    def test_a_wait_ends_when_the_held_lease_expires(self, conn: sqlite3.Connection) -> None:
        """The holder crashed: nothing releases the claim, and the wait must still end early."""
        take_claim(conn, SESSION, FINGERPRINT, lease=timedelta(seconds=2), now=NOON, pid="111")
        clock = FakeClock(NOON)

        cleared = wait_for_claim(
            conn,
            SESSION,
            FINGERPRINT,
            timeout=timedelta(minutes=1),
            poll_interval=timedelta(seconds=1),
            clock=clock,
            sleep=clock.sleep,
        )

        assert cleared is None
        assert clock.now == NOON + timedelta(seconds=2)

    def test_waiting_on_a_session_nobody_claimed_returns_immediately(
        self, conn: sqlite3.Connection
    ) -> None:
        clock = FakeClock(NOON)

        assert wait_for_claim(conn, SESSION, FINGERPRINT, clock=clock, sleep=clock.sleep) is None
        assert clock.slept == []

    def test_a_sleep_that_does_not_advance_the_clock_raises_rather_than_spinning(
        self, conn: sqlite3.Connection
    ) -> None:
        """Never wait forever — not even when the injected dependency is broken."""
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111")
        clock = FakeClock(NOON)

        with pytest.raises(ClaimError, match="did not advance"):
            wait_for_claim(
                conn,
                SESSION,
                FINGERPRINT,
                timeout=timedelta(seconds=5),
                poll_interval=timedelta(seconds=1),
                clock=clock,
                sleep=clock.frozen_sleep,
            )

    def test_a_non_positive_poll_interval_is_refused(self, conn: sqlite3.Connection) -> None:
        take_claim(conn, SESSION, FINGERPRINT, lease=LEASE, now=NOON, pid="111")
        clock = FakeClock(NOON)

        with pytest.raises(ClaimError, match="positive interval"):
            wait_for_claim(
                conn,
                SESSION,
                FINGERPRINT,
                poll_interval=timedelta(0),
                clock=clock,
                sleep=clock.sleep,
            )
