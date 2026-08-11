"""Contract on invalidation detection and forced-reload attribution.

Two things are being fenced. First, the **tiering**: caching is a prefix match over
``tools`` -> ``system`` -> ``messages``, so a tool-set change re-writes everything including
every instruction file, while an instruction edit re-writes system and messages but leaves the
schemas cached. Getting that backwards is what produces the misattribution "CLAUDE.md got
expensive" instead of "adding that MCP server cost $X".

Second, **conservatism**: a turn-level join between a write charge and the content that
explains it is loose (median ratio 3.31), so the excess charged to an event is bounded twice
and floored. An unexplained write must never be pinned on an item — or on an event.
"""

from pathlib import Path

import pytest

from ccaudit.ingest.records import AttachmentRecord, ToolResultRecord, parse_transcript
from ccaudit.ingest.tokens import TokenQuantity
from ccaudit.model.invalidation import (
    InvalidationEvent,
    TurnWrite,
    detect_invalidations,
    forced_reload_micros_at,
    reload_details,
)
from ccaudit.model.residency import Sizer, Timeline, build_timeline
from tests.fixtures.builder import TranscriptBuilder


def fixed_sizer(tokens: int) -> Sizer:
    def size(_record: ToolResultRecord | AttachmentRecord) -> TokenQuantity:
        return TokenQuantity(tokens=tokens, basis="exact", confidence="high", method="fixture")

    return size


def parse(
    builder: TranscriptBuilder, tmp_path: Path, item_tokens: int = 10_000
) -> tuple[Timeline, list[AttachmentRecord]]:
    parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
    timeline = build_timeline(
        parsed.turns,
        parsed.tool_results,
        parsed.attachments,
        parsed.compactions,
        sizer=fixed_sizer(item_tokens),
    )
    return timeline, parsed.attachments


def event(
    *,
    tier: str = "tools",
    trigger: str = "tool_set_changed",
    forced_reload_micros: int = 0,
) -> InvalidationEvent:
    return InvalidationEvent(
        event_id="inval:s1:1",
        turn_index=1,
        tier=tier,
        trigger=trigger,
        detail="MCP server 'playwright' added (2 tools)",
        forced_reload_micros=forced_reload_micros,
        items_reloaded=0,
        basis="measured",
        confidence="high",
    )


class TestTiering:
    def test_a_tool_set_change_invalidates_all_three_tiers(self) -> None:
        """Tool schemas render first, so changing them re-writes the entire prompt — every
        instruction file included. This is why an added MCP server is the expensive change."""
        assert event(tier="tools").invalidated_tiers == ("tools", "system", "messages")

    def test_an_instruction_change_invalidates_system_and_messages_but_not_tools(self) -> None:
        """Instruction files render *after* the schemas, so the schemas stay cached."""
        assert event(tier="system", trigger="instruction_changed").invalidated_tiers == (
            "system",
            "messages",
        )

    def test_a_model_switch_invalidates_everything(self) -> None:
        """A different model is a different cache; nothing carries over."""
        assert event(tier="tools", trigger="model_switched").invalidated_tiers == (
            "tools",
            "system",
            "messages",
        )

    def test_an_unknown_tier_or_trigger_raises_rather_than_being_stored(self) -> None:
        with pytest.raises(ValueError, match="unknown tier"):
            event(tier="prompt")
        with pytest.raises(ValueError, match="unknown trigger"):
            event(trigger="vibes")


class TestDetection:
    def test_an_added_mcp_server_is_detected_and_named_as_a_user_would_name_it(
        self, tmp_path: Path
    ) -> None:
        """ "MCP server 'playwright' added", never "tier 0 invalidated" — a cause a reader
        cannot act on is not a finding."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_tool_schema_delta(
            added=["mcp__playwright__navigate", "mcp__playwright__click"], added_lines=400
        )
        builder.add_turn(cache_creation_5m=30_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert len(events) == 1
        assert events[0].tier == "tools"
        assert events[0].trigger == "tool_set_changed"
        assert events[0].detail == "MCP server 'playwright' added (2 tools)"

    def test_a_removed_plain_tool_is_named_too(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_tool_schema_delta(added=[], removed=["NotebookEdit"])
        builder.add_turn(cache_creation_5m=1_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert events[0].detail == "tool removed: NotebookEdit"

    def test_a_model_switch_between_consecutive_turns_is_detected(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(model="claude-opus-4-6", output_tokens=10)
        builder.add_turn(model="claude-opus-5", cache_creation_5m=20_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert events[0].trigger == "model_switched"
        assert events[0].detail == "model switched from claude-opus-4-6 to claude-opus-5"

    def test_a_skill_listing_change_lands_in_the_system_tier(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_skill_listing(names=["dataviz", "run"], content="x" * 400)
        builder.add_turn(cache_creation_5m=5_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert events[0].tier == "system"
        assert events[0].trigger == "instruction_changed"

    def test_the_first_turn_cannot_be_an_invalidation(self, tmp_path: Path) -> None:
        """The initial schemas and skill listing are a first load, not a change to something
        already cached. Charging them as a forced reload would invent a cause."""
        builder = TranscriptBuilder()
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=200)
        builder.add_skill_listing(names=["run"], content="x" * 400)
        builder.add_turn(cache_creation_5m=30_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        assert detect_invalidations(timeline, attachments, [], session_id="s1") == []

    def test_an_ordinary_turn_produces_no_event(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_creation_5m=10_000, cache_read=5_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        assert detect_invalidations(timeline, attachments, [], session_id="s1") == []

    def test_two_changes_on_one_turn_produce_one_event_so_the_reload_is_costed_once(
        self, tmp_path: Path
    ) -> None:
        """Two events on the same turn would charge the same re-write twice. The quieter
        change is named in the detail rather than costed."""
        builder = TranscriptBuilder()
        builder.add_turn(model="claude-opus-4-6", output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=200)
        builder.add_turn(model="claude-opus-5", cache_creation_5m=30_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert len(events) == 1
        # The model switch outranks the tool change; both are named.
        assert events[0].trigger == "model_switched"
        assert "MCP server 'playwright' added" in events[0].detail
        assert "model switched" in events[0].detail

    def test_a_duplicate_turn_in_the_write_ledger_raises(self, tmp_path: Path) -> None:
        """A repeated turn would double-count the write it carries (Principle I)."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_turn(output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        writes = [TurnWrite(1, 1_000, 1_000), TurnWrite(1, 1_000, 1_000)]
        with pytest.raises(ValueError, match="duplicate turn index"):
            detect_invalidations(timeline, attachments, writes, session_id="s1")


class TestForcedReloadCost:
    def test_the_reload_lands_on_the_event_and_the_reloaded_file_does_not_absorb_it(
        self, tmp_path: Path
    ) -> None:
        """FR-081. The honest finding is "adding that server cost $X", not "CLAUDE.md got
        expensive" — so the cost is carried by the event, targeting the change."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        builder.add_turn(cache_creation_5m=10_000, output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=400)
        builder.add_turn(cache_creation_5m=20_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path, item_tokens=10_000)

        writes = [TurnWrite(turn_index=2, write_micros=125_000, write_tokens=20_000)]
        events = detect_invalidations(timeline, attachments, writes, session_id="s1")

        assert len(events) == 1
        assert events[0].items_reloaded == 1  # CLAUDE.md was carried, not newly arrived
        assert events[0].forced_reload_micros > 0
        assert forced_reload_micros_at(events, 2) == events[0].forced_reload_micros
        # Nothing is charged to a turn that had no invalidation.
        assert forced_reload_micros_at(events, 1) == 0

    def test_the_excess_never_exceeds_what_could_have_been_rewritten(self, tmp_path: Path) -> None:
        """A write is routinely several times the size of the content that explains it (§5.3),
        so the excess is capped at the tokens that were actually resident to be re-written."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        builder.add_turn(cache_creation_5m=1_000, output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=400)
        # A write far larger than anything resident could account for.
        builder.add_turn(cache_creation_5m=500_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path, item_tokens=1_000)

        writes = [TurnWrite(turn_index=2, write_micros=1_000_000, write_tokens=500_000)]
        events = detect_invalidations(timeline, attachments, writes, session_id="s1")

        # Resident to be re-written: CLAUDE.md at 1,000 tokens out of a 500,000-token write.
        assert events[0].forced_reload_micros == 1_000_000 * 1_000 // 500_000
        assert events[0].forced_reload_micros < 1_000_000

    def test_an_unexplained_write_is_not_pinned_on_the_event(self, tmp_path: Path) -> None:
        """When arriving content already explains the whole write, the invalidation gets
        nothing — missing attribution beats wrong attribution (FR-019)."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=400)
        builder.add_turn(cache_creation_5m=5_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path, item_tokens=50_000)

        writes = [TurnWrite(turn_index=1, write_micros=60_000, write_tokens=5_000)]
        events = detect_invalidations(timeline, attachments, writes, session_id="s1")
        assert events[0].forced_reload_micros == 0

    def test_an_event_with_no_write_charged_reports_the_change_at_zero_cost(
        self, tmp_path: Path
    ) -> None:
        """The reader still wants to see the change happened; no cost is claimed for it."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=400)
        builder.add_turn(cache_read=5_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert events[0].forced_reload_micros == 0

    def test_a_costed_excess_is_never_presented_as_a_measurement(self, tmp_path: Path) -> None:
        """The token split is a bound, not a reading, so basis and confidence say so."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        builder.add_turn(cache_creation_5m=10_000, output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=400)
        builder.add_turn(cache_creation_5m=20_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path, item_tokens=10_000)

        writes = [TurnWrite(turn_index=2, write_micros=125_000, write_tokens=20_000)]
        events = detect_invalidations(timeline, attachments, writes, session_id="s1")
        assert events[0].basis == "estimated"
        assert events[0].confidence == "low"

    def test_a_negative_cost_cannot_be_stored(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            event(forced_reload_micros=-1)


class TestProvenance:
    def test_every_event_can_be_traced_to_the_record_that_caused_it(self, tmp_path: Path) -> None:
        """FR-015 — a skeptic checks "did that server really get added on turn 2?"."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=400)
        builder.add_turn(cache_creation_5m=20_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert events[0].source_refs
        assert events[0].event_id == "inval:s1:1"

    def test_reload_details_hands_the_lane_classifier_a_named_cause(self, tmp_path: Path) -> None:
        """The bridge to lanes.py: without a named cause a re-written carried item gets no
        lane at all, which is the honest answer but not a useful one."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        builder.add_tool_schema_delta(added=["mcp__playwright__navigate"], added_lines=400)
        builder.add_turn(cache_creation_5m=20_000, output_tokens=10)
        timeline, attachments = parse(builder, tmp_path)

        events = detect_invalidations(timeline, attachments, [], session_id="s1")
        assert reload_details(events) == {1: "MCP server 'playwright' added (1 tool)"}
