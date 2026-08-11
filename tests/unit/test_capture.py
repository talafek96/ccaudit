"""Contract on automatic capture.

Two things must hold, and they pull in opposite directions:

- **The hook returns immediately.** `SessionEnd` handlers share a 60-second budget that a
  plugin-supplied hook cannot raise for itself, so a 30-second analysis run inline would be
  silently cancelled on someone else's machine.
- **Nothing is lost by that.** The queue entry is the correctness guarantee; the detached
  worker is only a latency optimization.

And one rule over both: a failure here must never surface into the user's session (FR-054).
"""

import json
from pathlib import Path

import pytest

from ccaudit.capture import (
    QueueEntry,
    clear_queue,
    enqueue,
    queue_path,
    read_queue,
    release_worker_lock,
)


@pytest.fixture(autouse=True)
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Record spawn attempts instead of detaching a real process.

    Patched at `subprocess.Popen` rather than at `_spawn_worker`, so the lock logic under test
    still runs — patching the function out would have hidden the very thing it fences.
    """
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "ccaudit.capture.subprocess.Popen",
        lambda *args, **kwargs: calls.append(args),
    )
    return calls


class TestEnqueue:
    def test_it_writes_a_queue_entry(self, ccaudit_home: Path) -> None:
        assert enqueue("sess-1", "/tmp/s.jsonl") == 0
        entries = read_queue()
        assert len(entries) == 1
        assert entries[0].session_id == "sess-1"

    def test_it_never_analyses_inline(
        self, ccaudit_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason this module exists. Analysing here would be cancelled mid-run."""

        def explode(*_: object, **__: object) -> None:
            raise AssertionError("enqueue must not analyse")

        monkeypatch.setattr("ccaudit.analyse.analyse_transcript", explode)
        assert enqueue("sess-1", "/tmp/s.jsonl") == 0

    def test_repeated_hooks_append_rather_than_overwrite(self, ccaudit_home: Path) -> None:
        enqueue("sess-1", "/tmp/a.jsonl")
        enqueue("sess-2", "/tmp/b.jsonl")
        assert [entry.session_id for entry in read_queue()] == ["sess-1", "sess-2"]

    def test_nothing_to_enqueue_is_not_an_error(self, ccaudit_home: Path) -> None:
        assert enqueue(None, None) == 0
        assert read_queue() == []

    def test_it_always_reports_success_to_the_caller(
        self, ccaudit_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FR-054 — a failed background convenience must not interrupt someone's work.

        The failure is logged in full, not swallowed; it is simply not raised at the user.
        """

        def explode(_entry: QueueEntry) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("ccaudit.capture._append", explode)
        assert enqueue("sess-1", "/tmp/s.jsonl") == 0


class TestQueueDurability:
    def test_an_unreadable_line_is_skipped_not_fatal(self, ccaudit_home: Path) -> None:
        """A corrupt entry must not strand every other queued session."""
        enqueue("sess-1", "/tmp/a.jsonl")
        with queue_path().open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        enqueue("sess-2", "/tmp/b.jsonl")
        assert [entry.session_id for entry in read_queue()] == ["sess-1", "sess-2"]

    def test_an_absent_queue_is_empty_not_an_error(self, ccaudit_home: Path) -> None:
        assert read_queue() == []

    def test_clearing_is_safe_when_there_is_nothing_to_clear(self, ccaudit_home: Path) -> None:
        clear_queue()
        clear_queue()

    def test_the_entry_survives_a_round_trip(self, ccaudit_home: Path) -> None:
        enqueue("sess-1", "/tmp/a.jsonl")
        raw = json.loads(queue_path().read_text(encoding="utf-8").strip())
        assert raw["session_id"] == "sess-1"
        assert raw["reason"] == "session_end"
        assert raw["queued_at"]


class TestWorkerLock:
    def test_a_live_lock_prevents_a_second_worker(
        self, ccaudit_home: Path, spawned: list[tuple[object, ...]]
    ) -> None:
        """Two hooks firing close together must not start two workers over one queue."""
        enqueue("sess-1", "/tmp/a.jsonl")
        enqueue("sess-2", "/tmp/b.jsonl")
        assert len(spawned) == 1, "a second worker was spawned while the first still holds the lock"

    def test_a_worker_is_spawned_at_all(
        self, ccaudit_home: Path, spawned: list[tuple[object, ...]]
    ) -> None:
        """The detached run is what makes the result already there by the time you look."""
        enqueue("sess-1", "/tmp/a.jsonl")
        assert len(spawned) == 1

    def test_releasing_is_safe_when_it_was_never_taken(self, ccaudit_home: Path) -> None:
        release_worker_lock()
        release_worker_lock()
