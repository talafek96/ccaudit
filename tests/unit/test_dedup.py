"""Contract on dedup: each recorded exchange is counted exactly once (FR-021), always.

The failure this fences is silent inflation — a resumed or forked session replays earlier
exchanges into a new file, and summing both doubles figures that still look plausible.
"""

import json
from pathlib import Path

from claude_cost_tracker.ingest.dedup import DUPLICATE_TURN, USAGE_CONFLICT, dedup_turns
from claude_cost_tracker.ingest.records import ParsedTranscript, parse_transcript

MODEL = "claude-opus-5"


def _assistant_record(
    *,
    uuid: str,
    message_id: str,
    request_id: str,
    output_tokens: int = 100,
    cache_read_tokens: int = 1_000,
) -> dict[str, object]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "requestId": request_id,
        "sessionId": "session-a",
        "timestamp": "2026-08-11T10:00:00Z",
        "version": "2.1.220",
        "message": {
            "id": message_id,
            "model": MODEL,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": cache_read_tokens,
                "output_tokens": output_tokens,
            },
        },
    }


def _write_transcript(path: Path, records: list[dict[str, object]]) -> ParsedTranscript:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return parse_transcript(path)


def _total_output(transcripts: list[ParsedTranscript]) -> int:
    return sum(turn.usage.output_tokens for turn in dedup_turns(transcripts).turns)


class TestDedupCountsEachExchangeOnce:
    def test_resumed_session_replaying_earlier_turns_is_counted_once(self, tmp_path: Path) -> None:
        """A resume copies earlier exchanges into a new file; both files are on disk (FR-021)."""
        shared = _assistant_record(uuid="u1", message_id="msg_1", request_id="req_1")
        original = _write_transcript(
            tmp_path / "original.jsonl",
            [shared, _assistant_record(uuid="u2", message_id="msg_2", request_id="req_2")],
        )
        # The resumed file carries the same exchange under a fresh record uuid — which is why
        # the dedup key is (message id, request id) and not the record uuid.
        resumed = _write_transcript(
            tmp_path / "resumed.jsonl",
            [
                _assistant_record(uuid="u1-copy", message_id="msg_1", request_id="req_1"),
                _assistant_record(uuid="u3", message_id="msg_3", request_id="req_3"),
            ],
        )

        result = dedup_turns([original, resumed])

        assert [turn.dedup_key for turn in result.turns] == [
            ("msg_1", "req_1"),
            ("msg_2", "req_2"),
            ("msg_3", "req_3"),
        ]
        assert result.duplicates_dropped == 1
        assert result.input_count == 4

    def test_forked_session_shares_a_prefix_and_is_counted_once(self, tmp_path: Path) -> None:
        """A fork duplicates every exchange up to the branch point across two files."""
        prefix = [
            _assistant_record(uuid="u1", message_id="msg_1", request_id="req_1"),
            _assistant_record(uuid="u2", message_id="msg_2", request_id="req_2"),
        ]
        branch_a = _write_transcript(
            tmp_path / "branch-a.jsonl",
            [*prefix, _assistant_record(uuid="a3", message_id="msg_a3", request_id="req_a3")],
        )
        branch_b = _write_transcript(
            tmp_path / "branch-b.jsonl",
            [*prefix, _assistant_record(uuid="b3", message_id="msg_b3", request_id="req_b3")],
        )

        result = dedup_turns([branch_a, branch_b])

        assert len(result.turns) == 4
        assert result.duplicates_dropped == 2

    def test_reingesting_the_same_file_doubles_nothing(self, tmp_path: Path) -> None:
        """Ingest is idempotent: rescanning a corpus must not move a single figure."""
        transcript = _write_transcript(
            tmp_path / "session.jsonl",
            [
                _assistant_record(uuid="u1", message_id="msg_1", request_id="req_1"),
                _assistant_record(uuid="u2", message_id="msg_2", request_id="req_2"),
            ],
        )

        once = _total_output([transcript])
        twice = _total_output([transcript, parse_transcript(transcript.path)])

        assert once == 200
        assert twice == once

    def test_duplicates_within_one_file_are_collapsed(self, tmp_path: Path) -> None:
        """Compaction can replay an exchange inside the same file, not only across files."""
        transcript = _write_transcript(
            tmp_path / "session.jsonl",
            [
                _assistant_record(uuid="u1", message_id="msg_1", request_id="req_1"),
                _assistant_record(uuid="u1-again", message_id="msg_1", request_id="req_1"),
            ],
        )

        result = dedup_turns([transcript])

        assert len(result.turns) == 1
        assert result.duplicates_dropped == 1

    def test_records_without_api_identifiers_are_not_merged_together(self, tmp_path: Path) -> None:
        """Missing ids fall back to the record uuid — distinct records stay distinct."""
        first = _assistant_record(uuid="u1", message_id="msg_1", request_id="req_1")
        second = _assistant_record(uuid="u2", message_id="msg_2", request_id="req_2")
        for record in (first, second):
            message = record["message"]
            assert isinstance(message, dict)
            del message["id"]
            del record["requestId"]
        transcript = _write_transcript(tmp_path / "session.jsonl", [first, second])

        result = dedup_turns([transcript])

        assert len(result.turns) == 2
        assert result.duplicates_dropped == 0


class TestDedupReportsWhatItRemoved:
    def test_every_dropped_duplicate_is_traceable_to_both_files(self, tmp_path: Path) -> None:
        """Principle VI: a removed record is named with the file and line on both sides."""
        original = _write_transcript(
            tmp_path / "original.jsonl",
            [_assistant_record(uuid="u1", message_id="msg_1", request_id="req_1")],
        )
        resumed = _write_transcript(
            tmp_path / "resumed.jsonl",
            [_assistant_record(uuid="u1-copy", message_id="msg_1", request_id="req_1")],
        )

        result = dedup_turns([original, resumed])

        assert result.diagnostics[DUPLICATE_TURN].count == 1
        sample = result.diagnostics[DUPLICATE_TURN].samples[0]
        assert "resumed.jsonl:1" in sample
        assert "original.jsonl:1" in sample

    def test_conflicting_usage_on_one_key_is_surfaced_not_silently_chosen(
        self, tmp_path: Path
    ) -> None:
        """Two copies of one exchange charging differently is a finding, never a quiet pick."""
        original = _write_transcript(
            tmp_path / "original.jsonl",
            [
                _assistant_record(
                    uuid="u1", message_id="msg_1", request_id="req_1", output_tokens=100
                )
            ],
        )
        resumed = _write_transcript(
            tmp_path / "resumed.jsonl",
            [
                _assistant_record(
                    uuid="u1-copy", message_id="msg_1", request_id="req_1", output_tokens=999
                )
            ],
        )

        result = dedup_turns([original, resumed])

        assert len(result.turns) == 1
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.dedup_key == ("msg_1", "req_1")
        assert conflict.kept_usage.output_tokens == 100
        assert conflict.rejected_usage.output_tokens == 999
        assert result.diagnostics[USAGE_CONFLICT].count == 1

    def test_identical_duplicates_raise_no_conflict(self, tmp_path: Path) -> None:
        """The normal case — a faithful copy — must not generate noise."""
        record = _assistant_record(uuid="u1", message_id="msg_1", request_id="req_1")
        first = _write_transcript(tmp_path / "a.jsonl", [record])
        second = _write_transcript(tmp_path / "b.jsonl", [record])

        assert dedup_turns([first, second]).conflicts == []


class TestDedupIsDeterministic:
    def test_repeated_runs_produce_the_same_order_and_figures(self, tmp_path: Path) -> None:
        """Same input, byte-identical output, every run (FR-017, SC-009)."""
        first = _write_transcript(
            tmp_path / "a.jsonl",
            [
                _assistant_record(uuid=f"u{n}", message_id=f"msg_{n}", request_id=f"req_{n}")
                for n in range(10)
            ],
        )
        second = _write_transcript(
            tmp_path / "b.jsonl",
            [
                _assistant_record(uuid=f"v{n}", message_id=f"msg_{n}", request_id=f"req_{n}")
                for n in range(5, 15)
            ],
        )

        runs = [[turn.dedup_key for turn in dedup_turns([first, second]).turns] for _ in range(5)]

        assert all(run == runs[0] for run in runs)
        assert len(runs[0]) == 15

    def test_first_occurrence_wins(self, tmp_path: Path) -> None:
        """The kept record is the earliest-seen one; later copies are the carry-over."""
        first = _write_transcript(
            tmp_path / "a.jsonl",
            [_assistant_record(uuid="original", message_id="msg_1", request_id="req_1")],
        )
        second = _write_transcript(
            tmp_path / "b.jsonl",
            [_assistant_record(uuid="copy", message_id="msg_1", request_id="req_1")],
        )

        assert dedup_turns([first, second]).turns[0].uuid == "original"
        assert dedup_turns([second, first]).turns[0].uuid == "copy"

    def test_no_transcripts_yields_an_empty_result(self) -> None:
        """Nothing to dedup is a normal outcome, not an error."""
        result = dedup_turns([])

        assert result.turns == []
        assert result.duplicates_dropped == 0
        assert result.input_count == 0
