"""Automatic capture — enqueue a session for analysis, and get out of the way.

**The handler's whole job is to return.** ``SessionEnd`` hooks share an overall budget capped
at 60 seconds, and a hook supplied by an installed plugin **cannot raise that budget for
itself**. A full analysis is budgeted at up to 30 seconds (SC-005), so running it inline would
have it silently cancelled on someone else's machine. So: append a queue entry, spawn a
detached worker, exit.

**The queue is the correctness guarantee; the detached run is only a latency optimization.**
If the spawn fails, or the worker dies, or the platform cannot detach, the queue entry is still
there and the next invocation does the work. Nothing is lost — and nothing *can* be lost,
because every session remains fully analysable from its own records regardless (FR-087).

**Nothing here may surface an error into the user's session** (FR-054). Failures go to the log
file and the process exits successfully: a cost tool that interrupted someone's work to report
that it could not measure their work would be worse than not measuring it.
"""

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ccaudit.config import ccaudit_home

QUEUE_FILENAME = "queue.jsonl"
# Deliberately generous. A worker that is genuinely still running must not be duplicated, but a
# crashed one must not block the queue forever — this is the same reclaim-on-expiry idea as the
# analysis claim, applied to the spawn itself.
SPAWN_LOCK_SECONDS = 300

_LOGGER = logging.getLogger("ccaudit.capture")


@dataclass(frozen=True)
class QueueEntry:
    """One session waiting to be analysed."""

    session_id: str
    transcript_path: str
    queued_at: str
    reason: str = "session_end"

    def to_json(self) -> str:
        return json.dumps(
            {
                "session_id": self.session_id,
                "transcript_path": self.transcript_path,
                "queued_at": self.queued_at,
                "reason": self.reason,
            }
        )


def queue_path() -> Path:
    return ccaudit_home() / QUEUE_FILENAME


def enqueue(
    session_id: str | None, transcript_path: str | None, *, reason: str = "session_end"
) -> int:
    """Append a queue entry and spawn a detached worker. Returns an exit code.

    Always returns 0. The caller is a hook inside someone's editor session, and there is no
    failure here worth interrupting them for (FR-054).
    """
    try:
        if not session_id and not transcript_path:
            _LOGGER.info("nothing to enqueue: neither a session id nor a transcript path")
            return 0

        entry = QueueEntry(
            session_id=session_id or "",
            transcript_path=transcript_path or "",
            queued_at=datetime.now(UTC).isoformat(),
            reason=reason,
        )
        _append(entry)
        _spawn_worker()
    except Exception:
        # DELIBERATE DEVIATION from "narrow every except" (constitution Principle I), justified
        # here and nowhere else in the codebase. This runs inside the user's session-end path,
        # where FR-054 requires that a failure of a background convenience never surfaces as an
        # error in their editor. It is not swallowed: `.exception()` logs the full traceback to
        # $CCAUDIT_HOME/ccaudit.log, and the session stays analysable from its own records. The
        # alternative — interrupting someone's work to report that we could not measure it — is
        # strictly worse than not measuring it.
        _LOGGER.exception("enqueue failed; the session remains analysable from its records")
    return 0


def read_queue() -> list[QueueEntry]:
    """Every queued entry, oldest first. Unreadable lines are skipped and counted in the log."""
    path = queue_path()
    if not path.is_file():
        return []
    entries: list[QueueEntry] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
            entries.append(
                QueueEntry(
                    session_id=str(raw.get("session_id", "")),
                    transcript_path=str(raw.get("transcript_path", "")),
                    queued_at=str(raw.get("queued_at", "")),
                    reason=str(raw.get("reason", "session_end")),
                )
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            _LOGGER.warning("queue line %d is unreadable and was skipped", line_number)
    return entries


def clear_queue() -> None:
    """Drop the queue. Called once its entries have been analysed and stored."""
    path = queue_path()
    if path.is_file():
        path.unlink()


def _append(entry: QueueEntry) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Append-only and line-oriented so two hooks firing at once interleave whole lines rather
    # than corrupting each other's bytes.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.to_json() + "\n")


def _spawn_worker() -> None:
    """Start the analysis in a process that outlives this one.

    Windows has no ``start_new_session``; there the spawn is skipped and the queue entry does
    the work on the next invocation. That degradation is the design, not a gap.
    """
    if not hasattr(os, "setsid"):
        _LOGGER.info("detached spawn unavailable on this platform; queue entry will be processed")
        return

    lock = ccaudit_home() / "worker.lock"
    if _lock_is_live(lock):
        _LOGGER.info("a worker is already running; not spawning a second")
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")

    # Fixed argv, no shell, and no user-supplied string reaches it: the only variable is this
    # interpreter's own path.
    subprocess.Popen(
        [sys.executable, "-m", "ccaudit", "_process_queue"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _lock_is_live(lock: Path) -> bool:
    if not lock.is_file():
        return False
    try:
        stamped = datetime.fromisoformat(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (datetime.now(UTC) - stamped).total_seconds() < SPAWN_LOCK_SECONDS


def release_worker_lock() -> None:
    """Drop the spawn lock. Safe to call when it was never taken."""
    lock = ccaudit_home() / "worker.lock"
    if lock.is_file():
        lock.unlink()
