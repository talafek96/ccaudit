"""Contract on cache-lane classification.

The invariant this file exists to fence is **L1**: an item is called ``uncached`` only when it
is below the minimum cacheable prefix of *that turn's own model*, read from the pricing table.
A corpus spans models, so it spans thresholds, and the thresholds are not monotonic — so a
single session can price the same 984-token file at 0.1x on one turn and 1x on the next.

The second contract is honesty: the four cache-miss causes stay distinct, and where the
records cannot support a verdict this module returns none rather than a plausible one.
"""

from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

from ccaudit.config import BUNDLED_PRICING_PATH, UnknownModelError, load_pricing
from ccaudit.ingest.records import AttachmentRecord, ToolResultRecord, parse_transcript
from ccaudit.ingest.tokens import TokenQuantity
from ccaudit.model.lanes import (
    MAX_LOOKBACK_BLOCKS,
    LaneAssignment,
    classify_session,
)
from ccaudit.model.residency import Sizer, Timeline, build_timeline
from tests.fixtures.builder import TranscriptBuilder

PRICING = load_pricing(BUNDLED_PRICING_PATH)

# Measured on a real CLAUDE.md. It caches on Opus 5 (minimum 512) and does not on Opus 4.6
# (minimum 4096) — the same file, ~10x more per turn on one model than the other.
CLAUDE_MD_TOKENS = 984

SMALL_MODEL_THRESHOLD = "claude-opus-5"
LARGE_MODEL_THRESHOLD = "claude-opus-4-6"


def fixed_sizer(tokens: int, by_basename: Mapping[str, int] | None = None) -> Sizer:
    """A sizer with a default size and per-file overrides, keyed by basename.

    Sizes are injected rather than measured because the arithmetic under test here is the
    lane classification, not the token measurement — and a threshold test needs a file whose
    size sits on a known side of a known minimum.
    """
    overrides = dict(by_basename or {})

    def size(record: ToolResultRecord | AttachmentRecord) -> TokenQuantity:
        name = PurePosixPath(_identity_of(record)).name
        return TokenQuantity(
            tokens=overrides.get(name, tokens),
            basis="exact",
            confidence="high",
            method="fixture",
        )

    return size


def _identity_of(record: ToolResultRecord | AttachmentRecord) -> str:
    if isinstance(record, AttachmentRecord):
        return record.identity or record.attachment_type
    payload = record.payload
    if isinstance(payload, dict):
        file_block = payload.get("file")
        if isinstance(file_block, dict):
            return str(file_block.get("filePath", ""))
    return ""


def timeline_of(
    builder: TranscriptBuilder,
    tmp_path: Path,
    item_tokens: int,
    by_basename: Mapping[str, int] | None = None,
) -> Timeline:
    parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
    return build_timeline(
        parsed.turns,
        parsed.tool_results,
        parsed.attachments,
        parsed.compactions,
        sizer=fixed_sizer(item_tokens, by_basename),
    )


@pytest.fixture
def two_model_session() -> TranscriptBuilder:
    """One file, resident across a turn on Opus 5 and a turn on Opus 4.6."""
    builder = TranscriptBuilder()
    builder.add_turn(model=SMALL_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t1",))
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
    builder.add_turn(model=SMALL_MODEL_THRESHOLD, cache_creation_5m=CLAUDE_MD_TOKENS)
    builder.add_turn(
        model=LARGE_MODEL_THRESHOLD, input_tokens=CLAUDE_MD_TOKENS + 50, output_tokens=10
    )
    return builder


class TestThresholdIsPerTurnModel:
    def test_the_same_file_caches_on_one_model_and_not_the_other_in_one_session(
        self, two_model_session: TranscriptBuilder, tmp_path: Path
    ) -> None:
        """Invariant L1, and the reason it matters: a corpus spans models, so it spans
        thresholds, and the threshold is resolved per turn — never once per session."""
        timeline = timeline_of(two_model_session, tmp_path, CLAUDE_MD_TOKENS)
        classification = classify_session(timeline, PRICING)

        lanes = {a.turn_index: (a.lane, a.model) for a in classification.assignments}
        assert lanes[1] == ("loading", SMALL_MODEL_THRESHOLD)
        assert lanes[2] == ("uncached", LARGE_MODEL_THRESHOLD)

    def test_the_applied_threshold_is_recorded_on_every_verdict(
        self, two_model_session: TranscriptBuilder, tmp_path: Path
    ) -> None:
        """A reader re-derives the verdict from the report, without rerunning the tool."""
        timeline = timeline_of(two_model_session, tmp_path, CLAUDE_MD_TOKENS)
        classification = classify_session(timeline, PRICING)

        thresholds = {a.model: a.threshold_tokens for a in classification.assignments}
        assert thresholds[SMALL_MODEL_THRESHOLD] == PRICING.min_cacheable_tokens(
            SMALL_MODEL_THRESHOLD
        )
        assert thresholds[LARGE_MODEL_THRESHOLD] == PRICING.min_cacheable_tokens(
            LARGE_MODEL_THRESHOLD
        )
        assert thresholds[SMALL_MODEL_THRESHOLD] != thresholds[LARGE_MODEL_THRESHOLD]

    def test_an_uncached_verdict_cannot_be_constructed_without_a_threshold(self) -> None:
        """Invariant L1 is enforced at construction, so no code path can route around it."""
        with pytest.raises(ValueError, match="invariant L1"):
            LaneAssignment(
                turn_index=0,
                item_id="file:-:/repo/CLAUDE.md",
                model=SMALL_MODEL_THRESHOLD,
                lane="uncached",
                lane_reason="below_minimum",
                size_tokens=CLAUDE_MD_TOKENS,
                threshold_tokens=512,
                confidence="high",
            )

    def test_an_uncached_verdict_cannot_be_constructed_from_an_unknown_threshold(self) -> None:
        with pytest.raises(ValueError, match="no threshold"):
            LaneAssignment(
                turn_index=0,
                item_id="file:-:/repo/CLAUDE.md",
                model="claude-mystery-1",
                lane="uncached",
                lane_reason="below_minimum",
                size_tokens=10,
                threshold_tokens=None,
                confidence="high",
            )

    def test_an_unknown_model_raises_rather_than_being_classified_at_a_guess(
        self, tmp_path: Path
    ) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(model="claude-imaginary-9", output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 100)
        with pytest.raises(UnknownModelError):
            classify_session(timeline, PRICING)


class TestSubThresholdContent:
    def test_a_sub_threshold_item_is_full_rate_on_every_turn_it_is_resident(
        self, tmp_path: Path
    ) -> None:
        """Sub-threshold content never transitions: it is billed at 1x every single turn."""
        builder = TranscriptBuilder()
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        for _ in range(3):
            builder.add_turn(model=LARGE_MODEL_THRESHOLD, input_tokens=2_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, CLAUDE_MD_TOKENS)

        summary = classify_session(timeline, PRICING).summary_for("file:-:/repo/CLAUDE.md")
        assert summary is not None
        assert summary.turns_by_lane["uncached"] == 3
        assert summary.turns_by_lane["cached"] == 0
        assert summary.full_rate_token_turns == 3 * CLAUDE_MD_TOKENS
        assert summary.reduced_rate_token_turns == 0

    def test_never_cacheable_on_names_the_model(self, tmp_path: Path) -> None:
        """FR-078 — a ~10x per-turn difference is a finding, not a footnote."""
        builder = TranscriptBuilder()
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, input_tokens=2_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, CLAUDE_MD_TOKENS)

        summary = classify_session(timeline, PRICING).summary_for("file:-:/repo/CLAUDE.md")
        assert summary is not None
        assert summary.never_cacheable_on == (LARGE_MODEL_THRESHOLD,)
        assert summary.is_never_cacheable

    def test_a_file_above_the_minimum_is_not_flagged(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/big.md")
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, cache_creation_5m=50_000, output_tokens=10)
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, cache_read=50_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 50_000)

        summary = classify_session(timeline, PRICING).summary_for("file:-:/repo/big.md")
        assert summary is not None
        assert summary.never_cacheable_on == ()
        assert summary.turns_by_lane["loading"] == 1  # the turn it arrived
        assert summary.turns_by_lane["cached"] == 1  # and the turn it was carried

    def test_a_full_rate_charge_too_small_to_contain_the_item_lowers_confidence(
        self, tmp_path: Path
    ) -> None:
        """Observe, don't predict: when the observed full-rate charge cannot contain the
        sub-threshold content, the mechanism does not get to override the record."""
        builder = TranscriptBuilder()
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, input_tokens=4, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, CLAUDE_MD_TOKENS)

        uncached = [a for a in classify_session(timeline, PRICING).assignments if a.turn_index == 1]
        assert [a.lane for a in uncached] == ["uncached"]
        assert uncached[0].confidence == "low"

    def test_uncached_tokens_bound_what_sub_threshold_content_can_explain(
        self, tmp_path: Path
    ) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, input_tokens=2_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, CLAUDE_MD_TOKENS)

        classification = classify_session(timeline, PRICING)
        assert classification.uncached_tokens_at(1) == CLAUDE_MD_TOKENS


class TestMissingThreshold:
    def test_a_model_without_a_recorded_threshold_is_named_and_confidence_capped(
        self, tmp_path: Path
    ) -> None:
        """A missing threshold is a gap in the rate table, not a licence to assume it cached."""
        table = tmp_path / "pricing.toml"
        table.write_text(
            "schema_version = 1\n"
            'priced_on = "2026-08-11"\n'
            "[cache]\n"
            "read_multiplier = 0.1\n"
            "write_multiplier_5m = 1.25\n"
            "write_multiplier_1h = 2.0\n"
            "unknown_ttl_multiplier = 1.25\n"
            'unknown_ttl_confidence = "low"\n'
            '[models."claude-opus-5"]\n'
            "input_usd_per_mtok = 5.0\n"
            "output_usd_per_mtok = 25.0\n",
            encoding="utf-8",
        )
        pricing = load_pricing(table)

        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=5_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 5_000)

        classification = classify_session(timeline, pricing)
        assert classification.threshold_unknown_models == ("claude-opus-5",)
        assert all(a.confidence == "low" for a in classification.assignments)
        assert all(a.lane != "uncached" for a in classification.assignments)


class TestMissCauses:
    def test_the_four_causes_stay_distinct(self, tmp_path: Path) -> None:
        """FR-082. Each cause implies a different fix, so collapsing them destroys the advice.

        One session that produces all four: a sub-threshold file (never eligible), a
        compaction (evicted), a detected prefix change (invalidated), and a turn that adds more
        content blocks than a breakpoint can walk back over (lookback miss).
        """
        builder = TranscriptBuilder()
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t0", "t1"))
        builder.add_tool_result(tool_use_id="t0", file_path="/repo/CLAUDE.md")
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/big.md")
        # Turn 1: both arrive. CLAUDE.md is below the Opus 4.6 minimum, big.md is not.
        builder.add_turn(
            model=LARGE_MODEL_THRESHOLD,
            input_tokens=2_000,
            cache_creation_5m=60_000,
            output_tokens=10,
        )
        # Turn 2: a detected prefix change forces the carried content back in.
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, cache_creation_5m=60_000, output_tokens=10)
        for index in range(MAX_LOOKBACK_BLOCKS + 2):
            builder.add_tool_result(tool_use_id=f"b{index}", file_path=f"/repo/f{index}.py")
        # Turn 3: no cache read at all, and more blocks added than a breakpoint walks back.
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, cache_creation_5m=9_000, output_tokens=10)
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, input_tokens=2_000, output_tokens=10)
        builder.add_compaction(pre_tokens=90_000, post_tokens=1_000, preserved_uuids=[])
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, input_tokens=2_000, output_tokens=10)
        timeline = timeline_of(
            builder, tmp_path, 50_000, by_basename={"CLAUDE.md": CLAUDE_MD_TOKENS}
        )

        classification = classify_session(
            timeline, PRICING, forced_reload_turns={2: "MCP server 'playwright' added"}
        )
        counts = classification.misses_by_cause()
        assert counts["never_eligible"] > 0
        assert counts["evicted"] > 0
        assert counts["invalidated"] > 0
        assert counts["lookback_miss"] > 0

    def test_a_lookback_miss_is_not_claimed_without_the_structural_precondition(
        self, tmp_path: Path
    ) -> None:
        """The one cause with no direct record behind it, so it is claimed only when both the
        observed signature and the block count support it."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_creation_5m=9_000, output_tokens=10)
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/b.py")
        builder.add_turn(cache_creation_5m=9_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 5_000)

        classification = classify_session(timeline, PRICING)
        assert classification.misses_by_cause()["lookback_miss"] == 0

    def test_an_eviction_is_read_off_the_compaction_boundary(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=5_000, output_tokens=10)
        builder.add_compaction(pre_tokens=50_000, post_tokens=1_000, preserved_uuids=[])
        builder.add_turn(cache_read=1_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 5_000)

        evicted = [m for m in classify_session(timeline, PRICING).misses if m.cause == "evicted"]
        assert evicted
        assert evicted[0].confidence == "high"

    def test_every_miss_carries_a_sentence_a_reader_can_act_on(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/CLAUDE.md")
        builder.add_turn(model=LARGE_MODEL_THRESHOLD, input_tokens=2_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, CLAUDE_MD_TOKENS)

        misses = classify_session(timeline, PRICING).misses
        assert misses
        assert all(miss.detail and not miss.detail.startswith("tier") for miss in misses)


class TestNoVerdictWithoutEvidence:
    def test_a_carried_item_on_a_turn_with_no_read_and_no_cause_gets_no_lane(
        self, tmp_path: Path
    ) -> None:
        """The records do not say what happened, so nothing is asserted — the cost lands in the
        visible unattributed remainder instead of on a file (FR-019)."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_creation_5m=50_000, output_tokens=10)
        builder.add_turn(input_tokens=10, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 50_000)

        classification = classify_session(timeline, PRICING)
        assert [a.lane for a in classification.at(1)] == ["loading"]
        assert classification.at(2) == []

    def test_a_forced_reload_turn_puts_carried_content_in_the_loading_lane(
        self, tmp_path: Path
    ) -> None:
        """With a detected cause the re-write is explained, so the lane is claimed — its
        *cost* still belongs to the change, not to the content (FR-081)."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_creation_5m=50_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 50_000)

        classification = classify_session(
            timeline, PRICING, forced_reload_turns={1: "MCP server 'playwright' added"}
        )
        assert [(a.lane, a.lane_reason) for a in classification.at(1)] == [
            ("loading", "reload_forced")
        ]

    def test_arriving_content_with_no_write_charged_is_cached_not_loading(
        self, tmp_path: Path
    ) -> None:
        """CHANGED INVARIANT — reviewed and accepted 2026-08-11.

        This previously asserted `first_load` (the loading lane) at low confidence, on the
        reasoning that the content *did* arrive even if no write was charged for it.

        That inverts the project's governing rule. The turn charged a cache **read** and no
        cache **write**; whatever we inferred about arrivals, nothing was written. Putting the
        item in the write lane excluded it from the read pool it was actually being charged
        in, so its share of an *observed* charge silently fell into the unattributed remainder
        — the tool attributing less than its own evidence supports.

        Found in practice: a tool-schema delta disappeared from the report entirely, because
        it was ruled `loading` on a turn whose only charge was a read.

        Confidence stays low, which is the honest part of the original reasoning: the arrival
        and the charge disagree, most likely a cache breakpoint placed elsewhere, and a figure
        resting on that is not a measurement.
        """
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=50_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 50_000)

        arriving = classify_session(timeline, PRICING).at(1)
        assert [a.lane for a in arriving] == ["cached"]
        assert arriving[0].confidence == "low"


class TestLaneWeights:
    def test_a_lane_pool_is_divided_only_among_the_items_in_that_lane(self, tmp_path: Path) -> None:
        """Cost-model §5.2: attribution runs *within* a lane, never against one
        undifferentiated resident set."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1", "t2"))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/b.py")
        builder.add_turn(cache_creation_5m=20_000, output_tokens=10)
        builder.add_turn(cache_read=20_000, output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 10_000)

        classification = classify_session(timeline, PRICING)
        loading_ids, loading_weights = classification.lane_weights(1, "loading")
        cached_ids, cached_weights = classification.lane_weights(1, "cached")

        assert sorted(loading_ids) == ["file:-:/repo/a.py", "file:-:/repo/b.py"]
        assert loading_weights == [10_000, 10_000]
        assert cached_ids == [] and cached_weights == []

    def test_an_unknown_lane_name_raises(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10)
        timeline = timeline_of(builder, tmp_path, 100)
        with pytest.raises(ValueError, match="unknown lane"):
            classify_session(timeline, PRICING).lane_weights(0, "free")
