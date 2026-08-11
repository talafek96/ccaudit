"""Contract on the residency timeline — where carry cost comes from.

Carry is ~54% of spend against ~22% for the initial load, so these rules decide most of the
money. The two that matter most: content is paid for from the turn *after* it arrives, and
carry stops the moment content leaves.
"""

from pathlib import Path

import pytest

from ccaudit.ingest.records import AttachmentRecord, ToolResultRecord, parse_transcript
from ccaudit.ingest.tokens import TokenQuantity
from ccaudit.model.residency import Sizer, Timeline, build_timeline
from tests.fixtures.builder import TranscriptBuilder


def fixed_sizer(tokens: int = 1_000) -> Sizer:
    """A sizer with a known answer, so these tests fence the arithmetic, not the measurement."""

    def size(_record: ToolResultRecord | AttachmentRecord) -> TokenQuantity:
        return TokenQuantity(tokens=tokens, basis="exact", confidence="high", method="fixture")

    return size


def timeline_from(
    builder: TranscriptBuilder,
    tmp_path: Path,
    *,
    sizer: Sizer | None = None,
    project_path: str | None = None,
) -> Timeline:
    parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
    return build_timeline(
        parsed.turns,
        parsed.tool_results,
        parsed.attachments,
        parsed.compactions,
        sizer=sizer or fixed_sizer(),
        project_path=project_path,
    )


class TestResidencyStart:
    def test_content_is_paid_for_from_the_turn_after_it_arrives(self, tmp_path: Path) -> None:
        """A tool result produced after turn 0 is sent in the request for turn 1."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)

        assert timeline.resident_at(0) == []
        assert len(timeline.resident_at(1)) == 1

    def test_content_arriving_after_the_last_turn_is_never_an_item(self, tmp_path: Path) -> None:
        """It was never in a request, so it was never paid for."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        timeline = timeline_from(builder, tmp_path)
        assert timeline.items == {}


class TestResidencyEnd:
    def test_carry_stops_when_compaction_evicts_the_content(self, tmp_path: Path) -> None:
        """FR-004. Charging carry past eviction invents cost that was never billed."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        builder.add_compaction(pre_tokens=5_000, post_tokens=500, preserved_uuids=[])
        builder.add_turn(cache_read=100, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)

        assert len(timeline.resident_at(1)) == 1
        assert timeline.resident_at(2) == []
        assert timeline.spans[0].end_reason == "evicted"

    def test_content_named_as_preserved_survives_the_compaction(self, tmp_path: Path) -> None:
        """The boundary's own survivor list is authoritative — no heuristic needed."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        survivor = builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        builder.add_compaction(pre_tokens=5_000, post_tokens=500, preserved_uuids=[survivor])
        builder.add_turn(cache_read=100, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)

        assert len(timeline.resident_at(2)) == 1
        # It survived the boundary and then stayed to the end of the session — the point is
        # that it was not evicted.
        assert timeline.spans[0].end_reason == "session_end"

    def test_content_still_resident_at_the_end_has_an_open_span(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)
        assert timeline.spans[0].last_turn is None
        assert timeline.spans[0].end_reason == "session_end"

    def test_pre_compaction_clearing_is_tracked_as_a_named_residual(self, tmp_path: Path) -> None:
        """Claude Code clears older tool outputs before compacting, leaving no marker.

        The unexplained drop is recorded so it can be displayed as its own line, rather than
        being absorbed into carry where it would silently inflate every file's figure.
        """
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_compaction(
            pre_tokens=10_000, post_tokens=1_000, preserved_uuids=[], cumulative_dropped=20_000
        )
        builder.add_turn(output_tokens=10)
        timeline = timeline_from(builder, tmp_path)
        assert timeline.unexplained_dropped_tokens == 11_000


class TestCauseProfiles:
    def test_repeat_loads_produce_distinct_injections(self, tmp_path: Path) -> None:
        """ "Read 40 times" and "read once, carried 58 turns" have opposite remedies (US2)."""
        builder = TranscriptBuilder()
        for _ in range(3):
            builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
            builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)

        item_id = next(iter(timeline.items))
        assert timeline.load_count(item_id) == 3

    def test_a_reload_extends_residency_rather_than_starting_a_second_span(
        self, tmp_path: Path
    ) -> None:
        """The file never left, so its carry is continuous; the reload is direct cost."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(output_tokens=10, tool_use_ids=("t2",))
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)

        item_id = next(iter(timeline.items))
        assert len(timeline.spans_for(item_id)) == 1
        assert timeline.load_count(item_id) == 2

    def test_turns_resident_counts_the_carry_window(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        for _ in range(3):
            builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)

        item_id = next(iter(timeline.items))
        assert timeline.turns_resident(item_id) == 3


class TestItemIdentity:
    def test_at_mentions_become_items(self, tmp_path: Path) -> None:
        """An @-mention has no tool call; a tool-walk misses it entirely (FR-022)."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_at_mention(display_path="docs/spec.md", content="# Spec")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)
        assert any(item.identity == "docs/spec.md" for item in timeline.items.values())

    def test_tool_schema_deltas_become_resident_instruction_items(self, tmp_path: Path) -> None:
        """The largest resident block by far — roughly 50x a project's instruction file."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_tool_schema_delta(added=["playwright__navigate"], added_lines=400)
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)
        kinds = {item.kind for item in timeline.items.values()}
        assert "tool_schema" in kinds

    def test_a_bash_result_names_no_file_and_is_not_an_item(self, tmp_path: Path) -> None:
        """Its cost belongs to the conversation; a synthetic identity would be a fiction."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", tool_name="Bash", text="ok")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)
        assert timeline.items == {}

    def test_same_path_in_different_projects_stays_distinct(self, tmp_path: Path) -> None:
        """Attribution must not merge two files that happen to share a path (edge case)."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)

        one = timeline_from(builder, tmp_path, project_path="/projects/alpha")
        two = timeline_from(builder, tmp_path, project_path="/projects/beta")
        assert set(one.items) != set(two.items)

    def test_a_withheld_size_is_a_gap_not_a_zero_weight_item(self, tmp_path: Path) -> None:
        """An image whose header we cannot read must not be recorded as costing nothing."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path, sizer=fixed_sizer(0))
        assert timeline.items == {}


class TestDeterminism:
    def test_the_same_transcript_yields_the_same_timeline(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)

        first = timeline_from(builder, tmp_path)
        second = timeline_from(builder, tmp_path)
        assert first.weights_at(1) == second.weights_at(1)
        assert [s.span_id for s in first.spans] == [s.span_id for s in second.spans]

    def test_an_empty_session_is_a_valid_empty_timeline(self, tmp_path: Path) -> None:
        """A session with no file activity is a valid result, not an error (edge case)."""
        builder = TranscriptBuilder()
        builder.add_ui_noise(5)
        timeline = timeline_from(builder, tmp_path)
        assert timeline.turns == []
        assert timeline.items == {}


class TestWeights:
    def test_weights_line_up_with_item_ids(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1", "t2"))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/b.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_from(builder, tmp_path)

        item_ids, weights = timeline.weights_at(1)
        assert len(item_ids) == len(weights) == 2
        assert all(weight == 1_000 for weight in weights)

    def test_a_turn_before_anything_arrived_has_no_weights(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        timeline = timeline_from(builder, tmp_path)
        assert timeline.weights_at(0) == ([], [])


@pytest.mark.parametrize("preserved", [[], ["nonexistent-uuid"]])
def test_an_unnamed_item_is_evicted(tmp_path: Path, preserved: list[str]) -> None:
    """Everything before the boundary that is not named as preserved is gone."""
    builder = TranscriptBuilder()
    builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
    builder.add_turn(cache_read=1_000, output_tokens=10)
    builder.add_compaction(pre_tokens=5_000, post_tokens=500, preserved_uuids=preserved)
    builder.add_turn(cache_read=100, output_tokens=10)
    timeline = timeline_from(builder, tmp_path)
    assert timeline.resident_at(2) == []
