"""Contract on the attribution pass.

Two invariants dominate: output cost never reaches a file (A2), and whatever the split does,
the pieces still add up to what was observed (A1). Everything else is a judgement the report
has to be able to defend.
"""

from pathlib import Path

import pytest

from claude_cost_tracker.config import BUNDLED_PRICING_PATH, UnknownModelError, load_pricing
from claude_cost_tracker.ingest.records import AttachmentRecord, ToolResultRecord, parse_transcript
from claude_cost_tracker.ingest.tokens import TokenQuantity
from claude_cost_tracker.model.attribute import attribute_session, price_turn
from claude_cost_tracker.model.residency import Sizer, build_timeline
from tests.fixtures.builder import TranscriptBuilder

PRICING = load_pricing(BUNDLED_PRICING_PATH)


def sizer(tokens: int = 1_000) -> Sizer:
    def size(_record: ToolResultRecord | AttachmentRecord) -> TokenQuantity:
        return TokenQuantity(tokens=tokens, basis="exact", confidence="high", method="fixture")

    return size


def analyse(builder: TranscriptBuilder, tmp_path: Path, policy: str = "proportional"):
    parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
    timeline = build_timeline(
        parsed.turns, parsed.tool_results, parsed.attachments, parsed.compactions, sizer=sizer()
    )
    return attribute_session("s1", timeline, PRICING, policy=policy)


class TestPricingATurn:
    def test_prices_each_component_at_its_own_rate(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(
            model="claude-opus-5",
            input_tokens=1_000_000,
            cache_creation_5m=1_000_000,
            cache_read=1_000_000,
            output_tokens=1_000_000,
        )
        parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
        charges = price_turn(parsed.turns[0], 0, PRICING)

        assert charges.fresh_input_micros == 5_000_000  # $5/MTok at 1x
        assert charges.cache_write_micros == 6_250_000  # 1.25x at the 5-minute window
        assert charges.cache_read_micros == 500_000  # 0.1x
        assert charges.output_micros == 25_000_000  # $25/MTok

    def test_the_one_hour_window_doubles_the_write(self, tmp_path: Path) -> None:
        """A single session-wide write multiplier understates 1h writes by 60%."""
        builder = TranscriptBuilder()
        builder.add_turn(cache_creation_1h=1_000_000)
        parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
        assert price_turn(parsed.turns[0], 0, PRICING).cache_write_micros == 10_000_000

    def test_both_windows_at_once_is_two_known_windows_not_one_unknown(
        self, tmp_path: Path
    ) -> None:
        """CHANGED, deliberately. This previously asserted that a turn writing into *both*
        windows had an unknown TTL and capped its confidence to "low".

        That was wrong, and expensively so. The record states how many tokens went to each
        window, so nothing is unknown — but treating it as unknown priced the write at a
        blended guess *and* dragged every figure derived from it down to one significant
        figure. Since Claude Code writes into both windows on most turns, that was **every**
        item in the report: a $358.90 folder displayed as "$400", and a $269.35 one as "$300",
        which is what a user reported as "the pricings are unreliable".

        The invariant the old test was reaching for still holds and is asserted below: a window
        the record does *not* state caps confidence rather than being assumed.
        """
        builder = TranscriptBuilder()
        builder.add_turn(cache_creation_5m=1_000_000, cache_creation_1h=1_000_000)
        parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
        charges = price_turn(parsed.turns[0], 0, PRICING)
        assert charges.ttl_confidence_cap is None
        # Priced per window: 1.25x on the 5m tokens, 2x on the 1h tokens, not a blend.
        assert charges.cache_write_micros == 6_250_000 + 10_000_000

    def test_a_window_the_record_does_not_state_caps_confidence(self, tmp_path: Path) -> None:
        """The real unknown: a flat total with no breakdown. Assuming 5m understates a 1h
        write by 60%, so it is priced conservatively and the figure says it is a guess."""
        builder = TranscriptBuilder()
        builder.add_turn(cache_creation_unsplit=1_000)
        parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
        assert price_turn(parsed.turns[0], 0, PRICING).ttl_confidence_cap == "low"

    def test_an_unsplit_write_is_not_filed_under_a_window(self, tmp_path: Path) -> None:
        """It used to be recorded as 5m-shaped, which reported a guess at full confidence."""
        builder = TranscriptBuilder()
        builder.add_turn(cache_creation_unsplit=1_000)
        usage = parse_transcript(builder.write(tmp_path / "s.jsonl")).turns[0].usage
        assert usage.cache_creation_unknown_tokens == 1_000
        assert usage.cache_creation_5m_tokens == 0
        assert usage.cache_creation_tokens == 1_000

    def test_a_turn_with_no_write_has_no_ttl_caveat(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(cache_read=1_000)
        parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
        assert price_turn(parsed.turns[0], 0, PRICING).ttl_confidence_cap is None

    def test_an_unknown_model_raises_rather_than_being_priced_at_a_guess(
        self, tmp_path: Path
    ) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(model="claude-imaginary-9", output_tokens=100)
        parsed = parse_transcript(builder.write(tmp_path / "s.jsonl"))
        with pytest.raises(UnknownModelError):
            price_turn(parsed.turns[0], 0, PRICING)


class TestComponentTargets:
    def test_output_goes_to_the_exchange_never_to_a_file(self, tmp_path: Path) -> None:
        """Invariant A2 / FR-005."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=1_000, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=5_000, output_tokens=500)
        result = analyse(builder, tmp_path)

        outputs = [a for a in result.attributions if a.component == "output"]
        assert outputs
        assert all(a.target_kind == "prompt" and a.target_id is None for a in outputs)

    def test_fresh_input_is_conversation_overhead_not_a_file_charge(self, tmp_path: Path) -> None:
        """Separating the sub-threshold part needs cache lanes; until then, no file is guessed."""
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=5_000, output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(input_tokens=1_000, cache_read=5_000, output_tokens=10)
        result = analyse(builder, tmp_path)

        overhead = [a for a in result.attributions if a.component == "overhead"]
        assert overhead
        assert all(a.target_kind == "prompt" for a in overhead)

    def test_the_write_is_charged_to_what_arrived(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_creation_5m=10_000, output_tokens=10)
        result = analyse(builder, tmp_path)

        direct = [a for a in result.attributions if a.component == "direct"]
        assert len(direct) == 1
        assert direct[0].target_id.endswith("/repo/a.py")

    def test_the_reshow_charge_is_carry_across_the_cached_items(self, tmp_path: Path) -> None:
        """Both files must be settled in the cache — content being written pays the write
        rate, not the read rate, so it is not in the cached lane on the turn it arrives."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1", "t2"))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/b.py")
        builder.add_turn(cache_creation_5m=2_000, output_tokens=10)
        builder.add_turn(cache_read=20_000, output_tokens=10)
        result = analyse(builder, tmp_path)

        carry = [a for a in result.attributions if a.component == "carry"]
        assert len(carry) == 2
        assert carry[0].cost_micros == carry[1].cost_micros  # equal weights, equal shares


class TestConservation:
    @pytest.mark.parametrize("policy", ["proportional", "exclusive"])
    def test_attributions_never_exceed_what_was_charged(self, policy: str, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=300, cache_creation_5m=9_000, output_tokens=120)
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/b.py")
        builder.add_turn(input_tokens=17, cache_read=9_100, output_tokens=333)
        result = analyse(builder, tmp_path, policy=policy)

        attributed = sum(a.cost_micros for a in result.attributions)
        assert attributed <= result.total_micros

    def test_the_exclusive_policy_attributes_less_than_proportional(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1", "t2"))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/b.py")
        builder.add_turn(cache_creation_5m=2_000, output_tokens=10)
        builder.add_turn(cache_read=50_000, output_tokens=10)

        proportional = analyse(builder, tmp_path, policy="proportional")
        exclusive = analyse(builder, tmp_path, policy="exclusive")

        assert sum(a.cost_micros for a in proportional.attributions) > sum(
            a.cost_micros for a in exclusive.attributions
        )
        # The policy moves per-item figures within a fixed pool; it cannot change the total.
        assert proportional.total_micros == exclusive.total_micros


class TestSubagents:
    def test_subagent_turns_are_counted_once_and_noted(self, tmp_path: Path) -> None:
        """FR-009. The charge is real and stays in the total; what must not happen is
        counting it at both the child and the parent."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=100)
        builder.add_turn(output_tokens=50, is_sidechain=True, agent_id="researcher")
        result = analyse(builder, tmp_path)

        assert result.subagent_turns_rolled_up == 1
        assert len(result.charges) == 2
        output_total = sum(a.cost_micros for a in result.attributions if a.component == "output")
        assert output_total == sum(c.output_micros for c in result.charges)


class TestProvenance:
    def test_every_attribution_carries_a_basis_and_confidence(self, tmp_path: Path) -> None:
        """No nullable defaults — a figure without them cannot be judged (FR-014)."""
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=100, cache_creation_5m=1_000, output_tokens=50)
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_read=1_000, output_tokens=10)
        result = analyse(builder, tmp_path)

        assert result.attributions
        for attribution in result.attributions:
            assert attribution.basis in ("exact", "measured", "estimated")
            assert attribution.confidence in ("high", "medium", "low")

    def test_every_attribution_can_be_traced_to_a_record(self, tmp_path: Path) -> None:
        """FR-015 — a skeptic checks the number without rerunning the tool."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_creation_5m=1_000, cache_read=500, output_tokens=10)
        result = analyse(builder, tmp_path)

        assert all(a.source_refs for a in result.attributions)

    def test_carry_figures_are_not_claimed_as_measurements(self, tmp_path: Path) -> None:
        """A carry number is one defensible division of a shared charge, not a measurement."""
        builder = TranscriptBuilder()
        builder.add_turn(output_tokens=10, tool_use_ids=("t1",))
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/a.py")
        builder.add_turn(cache_creation_5m=1_000, output_tokens=10)
        builder.add_turn(cache_read=10_000, output_tokens=10)
        result = analyse(builder, tmp_path)

        carry = [a for a in result.attributions if a.component == "carry"]
        assert carry
        assert all(a.confidence != "high" for a in carry)
