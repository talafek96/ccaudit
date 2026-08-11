"""Golden test — two files, comparable cost, opposite causes (US2, SC-010).

This is the fixture that demonstrates the product's reason for existing. A ranking that shows
"these two files cost about the same" is a report. A ranking that also shows *why* — one read
six times, the other read once and then carried — is a tool, because the two have opposite
remedies:

- `hot.py` is expensive because it keeps being **re-read**. Read it once, or read less of it.
- `steady.md` is expensive because it is **carried**. Its size matters; its read count does not.

A diff here is a red alert, not a rebaseline. See `test_attribution_basic.py` for the rule.
"""

from collections import defaultdict
from pathlib import Path

import pytest

from ccaudit.analyse import analyse_transcript
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing

pytestmark = pytest.mark.golden

TRANSCRIPT = Path(__file__).parent / "fixtures" / "session_cause_profiles" / "transcript.jsonl"
PRICING = load_pricing(BUNDLED_PRICING_PATH)

HOT = "file:-:/repo/src/hot.py"
STEADY = "file:-:/repo/docs/steady.md"


@pytest.fixture(scope="module")
def analysis():
    return analyse_transcript(TRANSCRIPT, pricing=PRICING)


@pytest.fixture(scope="module")
def by_item(analysis) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"direct": 0, "carry": 0})
    for attribution in analysis.attribution.attributions:
        if attribution.target_kind == "item" and attribution.target_id:
            totals[attribution.target_id][attribution.component] += attribution.cost_micros
    return totals


class TestTheProfilesAreOpposite:
    def test_the_re_read_file_is_dominated_by_loading(self, by_item) -> None:
        hot = by_item[HOT]
        total = hot["direct"] + hot["carry"]
        assert hot["direct"] / total > 0.9

    def test_the_carried_file_is_dominated_by_keeping_loaded(self, by_item) -> None:
        steady = by_item[STEADY]
        total = steady["direct"] + steady["carry"]
        assert steady["carry"] / total > 0.6

    def test_the_two_splits_differ_measurably(self, by_item) -> None:
        """The whole point: same ballpark cost, opposite composition (US2 acceptance 4)."""
        hot_share = by_item[HOT]["direct"] / sum(by_item[HOT].values())
        steady_share = by_item[STEADY]["direct"] / sum(by_item[STEADY].values())
        assert hot_share - steady_share > 0.5

    def test_the_totals_are_close_enough_that_the_total_alone_cannot_separate_them(
        self, by_item
    ) -> None:
        """If one were ten times the other, the cause would not be the interesting part."""
        hot_total = sum(by_item[HOT].values())
        steady_total = sum(by_item[STEADY].values())
        assert 0.5 < hot_total / steady_total < 2.0


class TestTheFalsifiableClaim:
    def test_ranking_by_cost_reorders_ranking_by_read_count(self, analysis, by_item) -> None:
        """SC-010, the experiment that decides whether the tool measures anything new.

        By read count `hot.py` wins six to one. By attributed cost `steady.md` wins. If carry
        cost never reordered a ranking, the product thesis would be wrong — and this fixture
        would fail rather than quietly agreeing with a read counter.
        """
        by_reads = sorted((HOT, STEADY), key=lambda item: -analysis.timeline.load_count(item))
        by_cost = sorted((HOT, STEADY), key=lambda item: -sum(by_item[item].values()))

        assert by_reads[0] == HOT, "hot.py is read more often"
        assert by_cost[0] == STEADY, "steady.md costs more"
        assert by_reads != by_cost, "carry cost did not change the ranking"


class TestTheEvidenceBehindEachProfile:
    def test_the_re_read_file_records_every_load(self, analysis) -> None:
        assert analysis.timeline.load_count(HOT) == 6

    def test_the_carried_file_was_read_once(self, analysis) -> None:
        assert analysis.timeline.load_count(STEADY) == 1

    def test_the_carried_file_stayed_resident_far_longer(self, analysis) -> None:
        assert analysis.timeline.turns_resident(STEADY) > analysis.timeline.turns_resident(HOT)

    def test_compaction_ended_the_churn_but_not_the_carry(self, analysis) -> None:
        """The boundary preserves steady.md by name and drops the rest (FR-025)."""
        assert analysis.timeline.compaction_turns
        spans = {span.item_id: span for span in analysis.timeline.spans}
        assert spans[HOT].end_reason == "evicted"
        assert spans[STEADY].end_reason == "session_end"


class TestItStillAddsUp:
    def test_the_breakdown_reconciles(self, analysis) -> None:
        result = analysis.reconciliation
        assert result.attributed_micros + result.unattributed_micros == result.total_micros

    def test_the_figures_are_stable_across_runs(self) -> None:
        first = analyse_transcript(TRANSCRIPT, pricing=PRICING)
        second = analyse_transcript(TRANSCRIPT, pricing=PRICING)
        assert first.total_micros == second.total_micros
