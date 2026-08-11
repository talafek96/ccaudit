"""Contract on transcript parsing.

The traps fenced here were all measured against a real corpus (prior-art pass 2 §5): most
records are not conversation, `input_tokens` is not the prompt size, and content arrives by
routes other than tool results.
"""

from pathlib import Path

import pytest

from ccaudit.ingest.records import (
    TranscriptFormatError,
    Usage,
    parse_transcript,
)
from tests.fixtures.builder import TranscriptBuilder, simple_session


def write(builder: TranscriptBuilder, tmp_path: Path, name: str = "session.jsonl") -> Path:
    return builder.write(tmp_path / name)


class TestUsage:
    def test_prompt_size_is_the_sum_of_all_three_input_measures(self) -> None:
        """FR-083. `input_tokens` alone is the uncached remainder, not the conversation."""
        usage = Usage(
            input_tokens=4_000,
            cache_creation_5m_tokens=10_000,
            cache_read_tokens=500_000,
            output_tokens=1_200,
        )
        assert usage.prompt_tokens == 514_000
        assert usage.prompt_tokens != usage.input_tokens

    def test_cache_creation_totals_across_both_windows(self) -> None:
        usage = Usage(cache_creation_5m_tokens=100, cache_creation_1h_tokens=900)
        assert usage.cache_creation_tokens == 1_000

    def test_ttl_is_read_not_assumed(self) -> None:
        assert Usage(cache_creation_5m_tokens=100).ttl == "5m"
        assert Usage(cache_creation_1h_tokens=100).ttl == "1h"

    def test_mixed_windows_report_no_single_ttl(self) -> None:
        """Pricing must not pick one; the write multiplier differs by 60% between them."""
        assert Usage(cache_creation_5m_tokens=1, cache_creation_1h_tokens=1).ttl is None

    def test_no_write_means_no_ttl(self) -> None:
        assert Usage(cache_read_tokens=5_000).ttl is None


class TestParsingConversation:
    def test_parses_turns_with_usage(self, tmp_path: Path) -> None:
        path = write(simple_session(), tmp_path)
        parsed = parse_transcript(path)
        assert len(parsed.turns) == 2
        assert parsed.turns[0].usage.cache_creation_5m_tokens == 2_000
        assert parsed.turns[1].usage.cache_read_tokens == 2_100

    def test_carries_the_session_id(self, tmp_path: Path) -> None:
        path = write(simple_session(), tmp_path)
        assert parse_transcript(path).session_id is not None

    def test_ui_state_records_are_ignored_not_errors(self, tmp_path: Path) -> None:
        """~60% of a real transcript is UI state. Treating it as an error rejects the file."""
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=10, output_tokens=10)
        builder.add_ui_noise(20)
        parsed = parse_transcript(write(builder, tmp_path))
        assert len(parsed.turns) == 1
        assert parsed.ignored_count >= 20
        assert parsed.unparseable_count == 0

    def test_tool_results_are_captured(self, tmp_path: Path) -> None:
        path = write(simple_session(), tmp_path)
        parsed = parse_transcript(path)
        assert len(parsed.tool_results) == 1
        assert parsed.tool_results[0].tool_use_id == "t1"

    def test_plain_user_text_is_not_an_injection(self, tmp_path: Path) -> None:
        """Real typing has no item identity to attribute; its cost is conversation overhead."""
        builder = TranscriptBuilder()
        builder.add_user_text("hello")
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.tool_results == []

    def test_tool_use_ids_are_captured_for_joining(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=5, tool_use_ids=("a", "b"))
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.turns[0].tool_use_ids == ("a", "b")

    def test_subagent_turns_are_marked(self, tmp_path: Path) -> None:
        """Sidechain work rolls up to the parent and must not be double-counted (FR-009)."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_turn(output_tokens=10, is_sidechain=True, agent_id="researcher")
        parsed = parse_transcript(write(builder, tmp_path))
        assert [t.is_sidechain for t in parsed.turns] == [False, True]
        assert parsed.turns[1].agent_id == "researcher"

    def test_attribution_fields_are_exact_joins(self, tmp_path: Path) -> None:
        """These sit on the same record as usage, so cost-by-skill is a join, not a guess."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, attribution_skill="prior-art", attribution_agent="x")
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.turns[0].attribution_skill == "prior-art"
        assert parsed.turns[0].attribution_agent == "x"


class TestDedupKey:
    def test_uses_message_and_request_id(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=1, message_id="m1", request_id="r1")
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.turns[0].dedup_key == ("m1", "r1")

    def test_falls_back_to_uuid_so_records_are_not_merged(self, tmp_path: Path) -> None:
        """Without a fallback, every id-less record would collapse into one."""
        builder = TranscriptBuilder()
        builder.add_raw(
            {
                "type": "assistant",
                "uuid": "u1",
                "message": {"model": "claude-opus-5", "usage": {"output_tokens": 5}},
            }
        )
        builder.add_raw(
            {
                "type": "assistant",
                "uuid": "u2",
                "message": {"model": "claude-opus-5", "usage": {"output_tokens": 5}},
            }
        )
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.turns[0].dedup_key != parsed.turns[1].dedup_key


class TestAttachments:
    def test_at_mentions_are_captured(self, tmp_path: Path) -> None:
        """An @-mention has no tool call and no file_path — a tool-walk misses it entirely."""
        builder = TranscriptBuilder()
        builder.add_at_mention(display_path="docs/spec.md", content="# Spec\n")
        parsed = parse_transcript(write(builder, tmp_path))
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].identity == "docs/spec.md"
        assert parsed.attachments[0].text_length > 0

    def test_skill_listing_is_captured(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_skill_listing(["a", "b"], content="skill listing text")
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.attachments[0].attachment_type == "skill_listing"

    def test_tool_schema_deltas_are_captured(self, tmp_path: Path) -> None:
        """The largest resident block, and the change that forces the costliest reloads."""
        builder = TranscriptBuilder()
        builder.add_tool_schema_delta(added=["playwright__navigate"], added_lines=400)
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.attachments[0].attachment_type == "deferred_tools_delta"
        assert parsed.attachments[0].payload["addedNames"] == ["playwright__navigate"]

    def test_non_content_attachments_are_not_items(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_attachment("date_change", {"newDate": "2026-08-12"})
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.attachments == []


class TestCompaction:
    def test_parses_the_boundary_and_what_survived(self, tmp_path: Path) -> None:
        """preservedMessages is authoritative — residency resets exactly, not heuristically."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_compaction(pre_tokens=263_315, post_tokens=11_490, preserved_uuids=["a", "b"])
        parsed = parse_transcript(write(builder, tmp_path))
        assert len(parsed.compactions) == 1
        compaction = parsed.compactions[0]
        assert compaction.pre_tokens == 263_315
        assert compaction.dropped_tokens == 251_825
        assert compaction.preserved_uuids == frozenset({"a", "b"})

    def test_boundary_carries_a_logical_parent_not_a_parent(self, tmp_path: Path) -> None:
        """Following only parentUuid splits the session at every compaction."""
        builder = TranscriptBuilder()
        first = builder.add_turn(output_tokens=10)
        builder.add_compaction(pre_tokens=100, post_tokens=10, preserved_uuids=[])
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.compactions[0].parent_uuid is None
        assert parsed.compactions[0].logical_parent_uuid == first

    def test_a_boundary_without_metadata_is_a_diagnostic(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_raw({"type": "system", "subtype": "compact_boundary", "uuid": "c1"})
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.compactions == []
        assert "malformed_record" in parsed.diagnostics

    def test_other_system_subtypes_are_ignored(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_raw({"type": "system", "subtype": "turn_duration", "uuid": "s1"})
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.compactions == []
        assert parsed.unparseable_count == 0


class TestDiagnostics:
    def test_unparseable_lines_are_counted_never_dropped(self, tmp_path: Path) -> None:
        """FR-027. A silently skipped record is a figure that is quietly wrong."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_malformed_line()
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.unparseable_count == 1
        assert "unparseable_json" in parsed.diagnostics

    def test_a_diagnostic_names_the_file_and_line(self, tmp_path: Path) -> None:
        """Every failure carries enough to triage it (Principle I)."""
        builder = TranscriptBuilder()
        builder.add_malformed_line()
        parsed = parse_transcript(write(builder, tmp_path, "target.jsonl"))
        sample = parsed.diagnostics["unparseable_json"].samples[0]
        assert "target.jsonl:1" in sample

    def test_samples_are_capped_so_a_broken_file_does_not_flood(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        for _ in range(50):
            builder.add_malformed_line()
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.diagnostics["unparseable_json"].count == 50
        assert len(parsed.diagnostics["unparseable_json"].samples) == 5

    def test_a_record_without_a_type_is_a_diagnostic(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_raw({"uuid": "x"})
        parsed = parse_transcript(write(builder, tmp_path))
        assert "missing_type" in parsed.diagnostics

    def test_usage_without_a_model_cannot_be_priced_and_is_reported(self, tmp_path: Path) -> None:
        """Pricing an unknown model is the confidently-wrong figure we refuse to produce."""
        builder = TranscriptBuilder()
        builder.add_raw(
            {"type": "assistant", "uuid": "a1", "message": {"usage": {"output_tokens": 10}}}
        )
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.turns == []
        assert "malformed_record" in parsed.diagnostics
        assert "no model" in parsed.diagnostics["malformed_record"].samples[0]


class TestVersionTracking:
    def test_versions_carry_forward_across_records_that_lack_one(self, tmp_path: Path) -> None:
        """`version` is absent on ~27% of records, so a per-row read under-reports."""
        builder = TranscriptBuilder(version="2.1.220")
        builder.add_turn(output_tokens=10)
        builder.add_ui_noise(5)
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.producing_versions == {"2.1.220"}

    def test_a_session_spanning_versions_says_so(self, tmp_path: Path) -> None:
        """FR-028 — a comparison crossing a version boundary must be identifiable."""
        builder = TranscriptBuilder(version="2.1.212")
        builder.add_turn(output_tokens=10)
        builder.add_turn(output_tokens=10, version="2.1.220")
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.spans_versions
        assert parsed.producing_versions == {"2.1.212", "2.1.220"}


class TestIterations:
    def test_iterations_are_not_summed_on_top_of_the_total(self, tmp_path: Path) -> None:
        """The top level is already rolled up; summing both double-counts output."""
        builder = TranscriptBuilder()
        builder.add_raw(
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "model": "claude-opus-5",
                    "usage": {
                        "output_tokens": 100,
                        "iterations": [{"output_tokens": 60}, {"output_tokens": 40}],
                    },
                },
            }
        )
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.turns[0].usage.output_tokens == 100

    def test_a_broken_rollup_is_surfaced_not_silently_miscounted(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_raw(
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {
                    "model": "claude-opus-5",
                    "usage": {
                        "output_tokens": 100,
                        "iterations": [{"output_tokens": 60}, {"output_tokens": 90}],
                    },
                },
            }
        )
        parsed = parse_transcript(write(builder, tmp_path))
        assert parsed.turns == []
        assert "malformed_record" in parsed.diagnostics


class TestTokenCoercion:
    def test_a_non_numeric_token_count_raises_rather_than_being_zeroed(self) -> None:
        """A format change must surface, not silently become a zero in someone's total."""
        from ccaudit.ingest.records import _as_int

        with pytest.raises(TranscriptFormatError, match="expected a token count"):
            _as_int("lots")

    def test_a_negative_token_count_raises(self) -> None:
        from ccaudit.ingest.records import _as_int

        with pytest.raises(TranscriptFormatError, match="negative"):
            _as_int(-5)

    def test_a_missing_count_is_zero(self) -> None:
        from ccaudit.ingest.records import _as_int

        assert _as_int(None) == 0


class TestReadOnly:
    def test_parsing_never_modifies_the_transcript(self, tmp_path: Path) -> None:
        """~/.claude/ is read-only input, always (FR-020)."""
        path = write(simple_session(), tmp_path)
        before = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns
        parse_transcript(path)
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == before_mtime

    def test_parsing_is_deterministic(self, tmp_path: Path) -> None:
        path = write(simple_session(), tmp_path)
        first = parse_transcript(path)
        second = parse_transcript(path)
        assert [t.dedup_key for t in first.turns] == [t.dedup_key for t in second.turns]
        assert first.record_count == second.record_count
