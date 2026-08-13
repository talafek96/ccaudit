"""Serving a finished analysis from the store instead of recomputing it (FR-104–FR-110).

Analysis is a pure function of the transcript, so a cache here can only ever cost time, never
correctness — *provided* the key covers everything the figures depend on. That is the whole of
the risk, and it is where the care goes:

- the **records** (`fingerprint`),
- the **split** (`policy`),
- the **rates** (`pricing_fingerprint`) — refreshable at any time (FR-099), so a figure priced
  by a superseded table is a wrong number rather than an old one,
- the **code** (`tool_version`) — a change to the model is a change to the conclusion.

A miss on any of them recomputes. Missing costs seconds; serving a stale figure costs the
thing the tool exists for.

Two further guards, because a cache is exactly where a quiet corruption would hide:

**The blob is checked on the way out, not only on the way in.** A restored contribution
re-derives its own reconciliation before it is handed back (invariant S2). A cache that returns
a total nobody re-checked is a second source of truth.

**Any doubt discards.** A row that will not decode — written by an older model shape, truncated,
corrupted — is treated as absent, not as an error to surface. It is a cache: throwing it away
is always safe and always correct (invariant S3, FR-110).
"""

import json
import sqlite3
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from hashlib import sha256
from pathlib import Path

from claude_cost_tracker import __version__
from claude_cost_tracker.analyse import SessionContribution
from claude_cost_tracker.ingest.discover import Fingerprint
from claude_cost_tracker.store.codec import UnsupportedTypeError, decode, encode
from claude_cost_tracker.store.db import transaction
from claude_cost_tracker.store.results import iso_timestamp, result_id_for

# JSON of a long session is highly repetitive — the same item ids and lane names, once per turn
# — and compresses about 17x on real transcripts. Without it a 900-session corpus would cache
# to over a gigabyte, which is not a cache anyone would keep.
_COMPRESSION_LEVEL = 6


@cache
def build_fingerprint() -> str:
    """What produced these figures: the release version, plus a hash of the code behind it.

    Computed once per process from the package's own source. Reading ~90 small files costs a
    few milliseconds; serving a figure derived by code that no longer exists costs the thing
    this tool is for.

    Falls back to the version alone if the source cannot be read — a packaging arrangement
    without readable sources should degrade to the old behaviour, not fail to start.
    """
    root = Path(__file__).resolve().parent.parent
    digest = sha256()
    try:
        for path in sorted(root.rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    except OSError:
        return __version__
    return f"{__version__}+{digest.hexdigest()[:12]}"


@dataclass(frozen=True)
class CacheKey:
    """Everything a stored figure depends on. A difference in any part is a miss."""

    session_id: str
    fingerprint: str
    policy: str
    pricing_fingerprint: str
    # Not the release version alone. `__version__` is a literal in `pyproject.toml` that
    # nothing bumps automatically, so a build that changed how a number is derived kept the
    # same key and every fix stayed invisible behind stale rows — measured on a real corpus,
    # 166 items served in an id format the current code cannot even produce. The build
    # fingerprint below moves whenever the code that produces the figures moves, which is the
    # property this field was always documented as having.
    tool_version: str = field(default_factory=lambda: build_fingerprint())


def cache_key(
    session_id: str,
    fingerprint: Fingerprint | str,
    policy: str,
    pricing_fingerprint: str,
) -> CacheKey:
    return CacheKey(
        session_id=session_id,
        fingerprint=str(fingerprint),
        policy=policy,
        pricing_fingerprint=pricing_fingerprint,
    )


def store_contribution(
    conn: sqlite3.Connection,
    key: CacheKey,
    contribution: SessionContribution,
    *,
    computed_at: datetime | None = None,
) -> None:
    """Cache a finished conclusion under ``key``.

    Checked before it is written: a contribution that does not reconcile is not stored, because
    a bad row would then be served fast and forever.
    """
    contribution.check_reconciles()
    blob = zlib.compress(
        json.dumps(encode(contribution, SessionContribution)).encode("utf-8"),
        _COMPRESSION_LEVEL,
    )
    moment = iso_timestamp(computed_at or datetime.now(UTC))
    result_id = result_id_for(key.session_id, key.policy, key.fingerprint)
    with transaction(conn):
        # `analysis_result.session_id` is a foreign key, so the session row has to exist first.
        # Only the identity is written here: everything else about the session belongs to the
        # normalised write path, and inventing half a row would make the store disagree with
        # itself about what it knows.
        conn.execute(
            "INSERT INTO session (session_id) VALUES (?) ON CONFLICT (session_id) DO NOTHING",
            (key.session_id,),
        )
        conn.execute(
            "INSERT INTO analysis_result (result_id, session_id, fingerprint, "
            "covered_through_turn, policy, producing_version, tool_version, computed_at, "
            "pricing_fingerprint, contribution) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (session_id, fingerprint, policy) DO UPDATE SET "
            "tool_version = excluded.tool_version, computed_at = excluded.computed_at, "
            "pricing_fingerprint = excluded.pricing_fingerprint, "
            "contribution = excluded.contribution",
            (
                result_id,
                key.session_id,
                key.fingerprint,
                len(contribution.timeline.turns),
                key.policy,
                None,
                key.tool_version,
                moment,
                key.pricing_fingerprint,
                blob,
            ),
        )


def read_contribution(conn: sqlite3.Connection, key: CacheKey) -> SessionContribution | None:
    """The cached conclusion for ``key``, or ``None`` — a normal outcome, not a failure.

    ``None`` covers every way this can fail to produce a trustworthy value: no row, a row for
    different records, different rates, a different build, an undecodable blob, or a blob that
    does not reconcile. They are one outcome on purpose. The caller's response to all of them
    is the same — compute it — and distinguishing them would invite a branch that serves
    something questionable rather than paying for a recomputation.
    """
    row = conn.execute(
        "SELECT contribution FROM analysis_result WHERE session_id = ? AND fingerprint = ? "
        "AND policy = ? AND pricing_fingerprint = ? AND tool_version = ? "
        "AND contribution IS NOT NULL",
        (
            key.session_id,
            key.fingerprint,
            key.policy,
            key.pricing_fingerprint,
            key.tool_version,
        ),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(zlib.decompress(row["contribution"]).decode("utf-8"))
        contribution = decode(payload, SessionContribution)
        contribution.check_reconciles()
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError, UnsupportedTypeError, ValueError):
        # A cache entry is never worth defending. Discarding it costs one recomputation;
        # trusting a damaged one costs a wrong number (invariant S3).
        return None
    return contribution
