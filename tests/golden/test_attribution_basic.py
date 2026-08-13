"""Golden test — the attribution arithmetic, pinned against hand-verified figures.

**A diff here is a red alert, never a rebaseline-by-default.** It means one of two things: a
real regression, or a deliberate change to the cost model. The second requires the derivation
in `expected.md` to be redone by hand first, and written human sign-off (constitution
Principle V). Do not "update the golden to match" — that is how a silently wrong number ships.

This is the highest-risk code in the project precisely because it fails *quietly*: a
misattribution produces a complete, plausible-looking report rather than a crash.
"""

import json
from collections import defaultdict
from pathlib import Path

import pytest

from claude_cost_tracker.analyse import analyse_transcript
from claude_cost_tracker.config import BUNDLED_PRICING_PATH, load_pricing

pytestmark = pytest.mark.golden

FIXTURE = Path(__file__).parent / "fixtures" / "session_basic"
TRANSCRIPT = FIXTURE / "transcript.jsonl"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))

# The golden is pinned to the rates it was hand-computed against. A pricing refresh changes
# every figure in it, which is correct and must be visible — not absorbed by reading whatever
# table happens to be in effect on the machine running the tests.
PRICING = load_pricing(BUNDLED_PRICING_PATH)


@pytest.fixture(scope="module")
def analysis():
    return analyse_transcript(TRANSCRIPT, pricing=PRICING, policy=EXPECTED["policy"])


class TestSessionTotals:
    def test_the_session_total_is_unchanged(self, analysis) -> None:
        assert analysis.total_micros == EXPECTED["total_micros"]

    def test_the_attributed_and_unattributed_split_is_unchanged(self, analysis) -> None:
        assert analysis.reconciliation.attributed_micros == EXPECTED["attributed_micros"]
        assert analysis.reconciliation.unattributed_micros == EXPECTED["unattributed_micros"]

    def test_it_adds_up(self, analysis) -> None:
        """Invariant A1 / SC-001, on the fixture whose arithmetic was checked by hand."""
        assert (
            analysis.reconciliation.attributed_micros + analysis.reconciliation.unattributed_micros
            == EXPECTED["total_micros"]
        )

    def test_the_unattributed_remainder_is_real_and_not_zero(self, analysis) -> None:
        """A golden with nothing unattributed would not fence the remainder path at all.

        Here the last turn's re-show charge arrives after a compaction evicted everything it
        could have been attributed to — cost with no resident item to explain it.
        """
        assert analysis.reconciliation.unattributed_micros == 1_000
        assert analysis.reconciliation.unattributed_share == pytest.approx(0.0255, abs=0.0005)


class TestPerTurnCharges:
    def test_every_turn_is_priced_as_derived_by_hand(self, analysis) -> None:
        actual = [
            {
                "turn": charge.turn_index,
                "fresh_input": charge.fresh_input_micros,
                "cache_write": charge.cache_write_micros,
                "cache_read": charge.cache_read_micros,
                "output": charge.output_micros,
                "total": charge.total_micros,
            }
            for charge in analysis.attribution.charges
        ]
        assert actual == EXPECTED["charges_by_turn"]

    def test_the_write_multiplier_is_applied(self, analysis) -> None:
        """1,000 tokens written at the 5-minute window is 6,250 micro-dollars, not 5,000."""
        assert analysis.attribution.charges[1].cache_write_micros == 6_250

    def test_the_read_rate_is_a_tenth(self, analysis) -> None:
        assert analysis.attribution.charges[4].cache_read_micros == 1_000  # 2,000 x 0.5


class TestPerItem:
    def test_item_sizes_are_unchanged(self, analysis) -> None:
        actual = {item_id: item.size_tokens for item_id, item in analysis.timeline.items.items()}
        assert actual == {
            item_id: entry["size_tokens"] for item_id, entry in EXPECTED["items"].items()
        }

    def test_categories_are_unchanged(self, analysis) -> None:
        actual = {item_id: item.category for item_id, item in analysis.timeline.items.items()}
        assert actual == {
            item_id: entry["category"] for item_id, entry in EXPECTED["items"].items()
        }

    def test_direct_and_carry_per_item_are_unchanged(self, analysis) -> None:
        totals: dict[str, dict[str, int]] = defaultdict(lambda: {"direct": 0, "carry": 0})
        for attribution in analysis.attribution.attributions:
            if attribution.target_kind == "item" and attribution.target_id:
                totals[attribution.target_id][attribution.component] += attribution.cost_micros

        for item_id, expected in EXPECTED["items"].items():
            assert totals[item_id]["direct"] == expected["direct"], item_id
            assert totals[item_id]["carry"] == expected["carry"], item_id

    def test_the_uneven_carry_split_conserves_its_pool(self, analysis) -> None:
        """Turn 3: 167 + 333 = 500. A naive floor gives 499 and loses a micro-dollar."""
        turn_three = [
            a
            for a in analysis.attribution.attributions
            if a.component == "carry" and a.turn_index == 3
        ]
        assert sum(a.cost_micros for a in turn_three) == 500
        assert sorted(a.cost_micros for a in turn_three) == [167, 333]

    def test_content_being_written_is_not_also_charged_the_read_rate(self, analysis) -> None:
        """Turn 2: b.md is being written, so the whole read charge belongs to a.py.

        Charging both would bill one piece of content twice, at two different rates, in one
        turn (docs/cost-model.md §5.2).
        """
        turn_two = [
            a
            for a in analysis.attribution.attributions
            if a.component == "carry" and a.turn_index == 2
        ]
        assert len(turn_two) == 1
        assert turn_two[0].target_id.endswith("/repo/src/a.py")
        assert turn_two[0].cost_micros == 500

    def test_load_counts_and_residency_are_unchanged(self, analysis) -> None:
        """The US2 distinction: read repeatedly vs read once and carried."""
        for item_id, expected in EXPECTED["items"].items():
            assert analysis.timeline.load_count(item_id) == expected["loads"], item_id
            assert analysis.timeline.turns_resident(item_id) == expected["turns_resident"], item_id


class TestPerComponent:
    def test_the_component_split_is_unchanged(self, analysis) -> None:
        totals: dict[str, int] = defaultdict(int)
        for attribution in analysis.attribution.attributions:
            totals[attribution.component] += attribution.cost_micros
        assert dict(totals) == EXPECTED["by_component"]

    def test_no_output_cost_reached_a_file(self, analysis) -> None:
        """Invariant A2 — pinned here as well as at the type level, because it is load-bearing."""
        outputs = [a for a in analysis.attribution.attributions if a.component == "output"]
        assert outputs
        assert all(a.target_kind == "prompt" for a in outputs)


class TestStability:
    def test_the_figures_are_identical_across_runs(self) -> None:
        """FR-017, SC-009 — byte-identical across runs, and therefore across machines."""
        first = analyse_transcript(TRANSCRIPT, pricing=PRICING)
        second = analyse_transcript(TRANSCRIPT, pricing=PRICING)
        assert [
            (a.turn_index, a.target_id, a.component, a.cost_micros)
            for a in first.attribution.attributions
        ] == [
            (a.turn_index, a.target_id, a.component, a.cost_micros)
            for a in second.attribution.attributions
        ]

    def test_the_fixture_is_synthetic(self) -> None:
        """Never a real transcript: they carry paths, commands, and source from real sessions."""
        text = TRANSCRIPT.read_text(encoding="utf-8")
        assert "golden-basic-0001" in text
        assert "/Users/" not in text
