"""Who is analysing what, so two runs do not duplicate work — and never block each other.

The correctness argument is short because **analysis is a pure function of the transcript**:
same records, same figures. A race therefore wastes CPU and cannot produce a different answer,
which reduces the problem from distributed locking to three obligations. Two of them live here:

**Never wait forever** (invariant K3, FR-091). :func:`wait_for_claim` waits a bounded interval
on a live claim and then hands control back so the caller computes the result itself. It acts
on observed state — it re-reads the claim each pass and re-reads the clock — rather than
sleeping a fixed span and hoping (Principle VI). The clock and the sleep are injected, so tests
exercise the real loop without spending real time.

**Never get stuck** (invariant K1, FR-092). A claim is a *lease*: it is taken by one atomic
statement that simultaneously reclaims any expired claim, so a process killed mid-analysis
blocks nothing past its expiry and there is no stale lock to clean up by hand. That is the
whole crash-recovery mechanism, and it is why ``expires_at`` is mandatory.

The third obligation — never write a partial result — lives in
:mod:`ccaudit.store.results`.

**File locks and a coordinating daemon were both rejected** (research §4): a stale ``flock``
after a kill needs exactly the manual cleanup FR-092 forbids, and a daemon violates the
local-first, no-infrastructure rule outright.
"""

import os
import socket
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ccaudit.ingest.discover import Fingerprint
from ccaudit.store.db import transaction
from ccaudit.store.results import iso_timestamp

__all__ = [
    "DEFAULT_LEASE",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_WAIT",
    "Claim",
    "ClaimError",
    "current_claim",
    "mark_done",
    "release_claim",
    "take_claim",
    "wait_for_claim",
]

# Long enough for a large session's analysis, short enough that a crashed run is forgotten
# within a coffee break. It is a lease, not a lock: over-running it costs a duplicate run, not
# a wrong answer.
DEFAULT_LEASE = timedelta(minutes=10)

# How long a reader is willing to wait on someone else's live claim before doing the work
# itself. Deliberately short — waiting longer than the analysis takes helps nobody.
DEFAULT_WAIT = timedelta(seconds=5)
DEFAULT_POLL_INTERVAL = timedelta(milliseconds=250)

# States the schema allows. `done` means the holder finished: the claim is a receipt, not a
# lease any more, so it never blocks the next actor.
RUNNING = "running"
DONE = "done"


class ClaimError(RuntimeError):
    """A claim could not be taken, released, or waited on coherently.

    Fatal by design (Principle I). Every message names the session and fingerprint, because a
    claim problem is otherwise invisible: nothing crashes, work just quietly does not happen.
    """


@dataclass(frozen=True)
class Claim:
    """One actor's lease on analysing a ``(session, fingerprint)``.

    Carries who holds it so anything else asking about this session can say so out loud rather
    than appearing to hang (FR-090).
    """

    session_id: str
    fingerprint: str
    state: str
    claimed_at: str
    expires_at: str
    pid: str
    host: str

    @property
    def holder(self) -> str:
        """Who to name in a message: "held by pid 4213 on box-a"."""
        return f"pid {self.pid} on {self.host}"

    def is_live(self, now: datetime) -> bool:
        """Whether this claim still holds. A finished or expired claim holds nothing."""
        return self.state != DONE and self.expires_at > iso_timestamp(now)

    def describe(self, now: datetime) -> str:
        """One line a caller can print verbatim when it finds this claim in its way."""
        if not self.is_live(now):
            state = "finished" if self.state == DONE else f"expired at {self.expires_at}"
            return f"claim on session {self.session_id} is {state} ({self.holder})"
        return (
            f"session {self.session_id} is being analysed by {self.holder}, "
            f"claimed at {self.claimed_at}, lease until {self.expires_at}"
        )


def take_claim(
    conn: sqlite3.Connection,
    session_id: str,
    fingerprint: Fingerprint | str,
    *,
    lease: timedelta = DEFAULT_LEASE,
    now: datetime | None = None,
    pid: str | None = None,
    host: str | None = None,
) -> Claim | None:
    """Claim this ``(session, fingerprint)``, reclaiming an expired or finished claim.

    Returns the claim on success, or ``None`` when someone else holds a live one — a normal
    outcome the caller branches on (wait, then compute anyway), not a failure.

    **One statement does both.** The insert-or-reclaim is a single upsert whose ``DO UPDATE``
    is conditional on the existing lease having lapsed, so two racing processes cannot both
    come away believing they hold it: SQLite applies the row change once, and the loser sees
    zero rows changed.
    """
    moment = now or datetime.now(UTC)
    if lease <= timedelta(0):
        raise ClaimError(
            f"session {session_id}: a lease of {lease} expires before it is taken, so it would "
            f"never hold anything. Pass a positive lease"
        )
    claimed_at = iso_timestamp(moment)
    expires_at = iso_timestamp(moment + lease)
    if expires_at <= claimed_at:
        raise ClaimError(
            f"session {session_id}: a lease of {lease} is shorter than the store's one-second "
            f"timestamp resolution, so it cannot be distinguished from an expired one"
        )

    with transaction(conn):
        # `claim.session_id` is a foreign key, and a claim is taken *before* the session has
        # been analysed and stored, so the session row may not exist yet. Creating the stub
        # here is what lets the claim precede the work it protects.
        conn.execute(
            "INSERT INTO session (session_id) VALUES (?) ON CONFLICT (session_id) DO NOTHING",
            (session_id,),
        )
        taken = conn.execute(
            "INSERT INTO claim (session_id, fingerprint, state, claimed_at, expires_at, pid, "
            "host) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (session_id, fingerprint) DO UPDATE SET "
            "state = excluded.state, claimed_at = excluded.claimed_at, "
            "expires_at = excluded.expires_at, pid = excluded.pid, host = excluded.host "
            "WHERE claim.expires_at <= excluded.claimed_at OR claim.state = ?",
            (
                session_id,
                str(fingerprint),
                RUNNING,
                claimed_at,
                expires_at,
                pid or str(os.getpid()),
                host or socket.gethostname(),
                DONE,
            ),
        ).rowcount

    if taken != 1:
        return None
    claim = current_claim(conn, session_id, fingerprint)
    if claim is None:  # pragma: no cover - the upsert above just committed this row
        raise ClaimError(
            f"session {session_id}: claim on fingerprint {fingerprint} was taken but is not "
            f"readable; the store is inconsistent"
        )
    return claim


def current_claim(
    conn: sqlite3.Connection, session_id: str, fingerprint: Fingerprint | str
) -> Claim | None:
    """The claim on this ``(session, fingerprint)``, live or not. ``None`` when there is none."""
    row = conn.execute(
        "SELECT session_id, fingerprint, state, claimed_at, expires_at, pid, host FROM claim "
        "WHERE session_id = ? AND fingerprint = ?",
        (session_id, str(fingerprint)),
    ).fetchone()
    if row is None:
        return None
    return Claim(
        session_id=row["session_id"],
        fingerprint=row["fingerprint"],
        state=row["state"],
        claimed_at=row["claimed_at"],
        expires_at=row["expires_at"],
        pid=row["pid"],
        host=row["host"],
    )


def mark_done(conn: sqlite3.Connection, session_id: str, fingerprint: Fingerprint | str) -> None:
    """Record that the analysis finished, so the next actor is not made to wait out the lease."""
    with transaction(conn):
        changed = conn.execute(
            "UPDATE claim SET state = ? WHERE session_id = ? AND fingerprint = ?",
            (DONE, session_id, str(fingerprint)),
        ).rowcount
    if changed != 1:
        raise ClaimError(
            f"session {session_id}: no claim on fingerprint {fingerprint} to mark done. Either "
            f"it was never taken, or it was reclaimed by another actor after expiring"
        )


def release_claim(
    conn: sqlite3.Connection, session_id: str, fingerprint: Fingerprint | str
) -> bool:
    """Drop the claim. Returns whether there was one — releasing twice is not an error."""
    with transaction(conn):
        removed = conn.execute(
            "DELETE FROM claim WHERE session_id = ? AND fingerprint = ?",
            (session_id, str(fingerprint)),
        ).rowcount
    return removed > 0


def wait_for_claim(
    conn: sqlite3.Connection,
    session_id: str,
    fingerprint: Fingerprint | str,
    *,
    timeout: timedelta = DEFAULT_WAIT,
    poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Claim | None:
    """Wait at most ``timeout`` for a live claim to clear (invariant K3, FR-091).

    Returns ``None`` once nothing holds the session — released, finished, or expired — and
    returns the still-live claim when the bound is reached, so the caller can name who has it
    and then compute the result itself. It never returns "clear" while a live claim exists,
    and it never waits past the bound.

    ``clock`` and ``sleep`` are injected so a test drives the real loop over a fake clock. A
    ``sleep`` that does not advance ``clock`` would spin forever, so it is treated as a broken
    dependency and raises rather than hanging.
    """
    if poll_interval <= timedelta(0):
        raise ClaimError(
            f"session {session_id}: poll interval {poll_interval} would spin without ever "
            f"yielding; pass a positive interval"
        )
    tick = clock or (lambda: datetime.now(UTC))
    deadline = tick() + timeout

    while True:
        claim = current_claim(conn, session_id, fingerprint)
        now = tick()
        if claim is None or not claim.is_live(now):
            return None
        remaining = deadline - now
        if remaining <= timedelta(0):
            return claim
        sleep(min(poll_interval, remaining).total_seconds())
        if tick() <= now:
            raise ClaimError(
                f"session {session_id}: the injected clock did not advance across a sleep, so "
                f"the bounded wait would never end. Fix the clock/sleep pair"
            )
