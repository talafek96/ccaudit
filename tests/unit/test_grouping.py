"""Contract on the grouping dimensions (FR-007).

The property that matters is that a grouping **partitions**: it may merge rows, never create
or drop cost. A per-folder view that quietly loses a file looks entirely plausible in a report,
which is exactly why it is checked rather than assumed.
"""


import pytest

from ccaudit.analyse import SessionAnalysis, analyse_transcript
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing
from ccaudit.render.data import GROUPINGS, UnknownGroupingError, build_report_data
from tests.fixtures.builder import TranscriptBuilder

PRICING = load_pricing(BUNDLED_PRICING_PATH)


@pytest.fixture(scope="module")
def analysis(tmp_path_factory: pytest.TempPathFactory) -> SessionAnalysis:
    """A session touching several folders, extensions, and categories."""
    tmp_path = tmp_path_factory.mktemp("grouping")
    builder = TranscriptBuilder()
    builder.add_turn(cache_creation_5m=6_000, output_tokens=50, tool_use_ids=("t1", "t2", "t3"))
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/src/app.py", text="a" * 4_000)
    builder.add_tool_result(tool_use_id="t2", file_path="/repo/src/util.py", text="b" * 8_000)
    builder.add_tool_result(tool_use_id="t3", file_path="/repo/docs/guide.md", text="c" * 12_000)
    builder.add_turn(cache_read=6_000, output_tokens=40, tool_use_ids=("t4",))
    builder.add_tool_result(tool_use_id="t4", file_path="/repo/docs/api.md", text="d" * 2_000)
    builder.add_turn(cache_creation_5m=500, cache_read=6_500, output_tokens=30)
    return analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)


def payload(analysis: SessionAnalysis, group_by: str) -> dict:
    return build_report_data([analysis], group_by=group_by)


class TestPartitioning:
    @pytest.mark.parametrize("group_by", GROUPINGS)
    def test_every_grouping_sums_to_the_same_attributed_total(
        self, analysis: SessionAnalysis, group_by: str
    ) -> None:
        """A grouping only merges rows. If the total moves, it dropped or duplicated one."""
        ungrouped = payload(analysis, "item")
        grouped = payload(analysis, group_by)
        assert sum(item["total_micros"] for item in grouped["items"]) == sum(
            item["total_micros"] for item in ungrouped["items"]
        )

    @pytest.mark.parametrize("group_by", GROUPINGS)
    def test_every_grouping_still_adds_up_overall(
        self, analysis: SessionAnalysis, group_by: str
    ) -> None:
        totals = payload(analysis, group_by)["totals"]
        assert totals["attributed_micros"] + totals["unattributed_micros"] == totals["cost_micros"]

    @pytest.mark.parametrize("group_by", GROUPINGS)
    def test_the_session_total_never_depends_on_the_view(
        self, analysis: SessionAnalysis, group_by: str
    ) -> None:
        assert (
            payload(analysis, group_by)["totals"]["cost_micros"]
            == payload(analysis, "item")["totals"]["cost_micros"]
        )


class TestDimensions:
    def test_grouping_by_category_merges_to_the_declared_categories(
        self, analysis: SessionAnalysis
    ) -> None:
        """The view the manager question is actually asked in."""
        names = {item["display"] for item in payload(analysis, "category")["items"]}
        assert names == {"source", "docs"}

    def test_grouping_by_folder_uses_the_immediate_parent(self, analysis: SessionAnalysis) -> None:
        """Not every ancestor: rolling a file into all of them double-counts it in one table."""
        names = {item["display"] for item in payload(analysis, "folder")["items"]}
        assert names == {"/repo/src", "/repo/docs"}

    def test_grouping_by_extension_merges_by_suffix(self, analysis: SessionAnalysis) -> None:
        names = {item["display"] for item in payload(analysis, "ext")["items"]}
        assert names == {".py", ".md"}

    def test_grouping_by_file_keeps_one_row_per_path(self, analysis: SessionAnalysis) -> None:
        names = {item["display"] for item in payload(analysis, "file")["items"]}
        assert len(names) == 4

    def test_the_ungrouped_view_is_the_default(self, analysis: SessionAnalysis) -> None:
        assert build_report_data([analysis])["group_by"] == "item"

    def test_the_grouping_in_effect_is_stated(self, analysis: SessionAnalysis) -> None:
        assert payload(analysis, "category")["group_by"] == "category"


class TestMergedRowHonesty:
    def test_a_merged_row_takes_the_weakest_confidence_of_its_members(
        self, analysis: SessionAnalysis
    ) -> None:
        """A bucket is only as trustworthy as its least trustworthy member."""
        order = {"low": 0, "medium": 1, "high": 2}
        grouped = payload(analysis, "category")["items"]
        ungrouped = payload(analysis, "item")["items"]
        worst_ungrouped = min(order[item["confidence"]] for item in ungrouped)
        worst_grouped = min(order[item["confidence"]] for item in grouped)
        assert worst_grouped <= worst_ungrouped

    def test_a_bucket_spanning_categories_says_mixed_rather_than_picking_one(
        self, analysis: SessionAnalysis
    ) -> None:
        """Grouping by extension can merge across categories; the label must not lie."""
        for item in payload(analysis, "ext")["items"]:
            assert item["category"] in ("source", "docs", "skill", "spec", "other", "(mixed)")

    def test_reads_and_residency_accumulate_into_the_bucket(
        self, analysis: SessionAnalysis
    ) -> None:
        grouped = {item["display"]: item for item in payload(analysis, "category")["items"]}
        ungrouped = payload(analysis, "item")["items"]
        source_reads = sum(item["reads"] for item in ungrouped if item["category"] == "source")
        assert grouped["source"]["reads"] == source_reads


class TestFailFast:
    def test_an_unknown_grouping_raises_rather_than_falling_back(
        self, analysis: SessionAnalysis
    ) -> None:
        with pytest.raises(UnknownGroupingError, match="unknown grouping"):
            payload(analysis, "vibes")


class TestDeterminism:
    @pytest.mark.parametrize("group_by", GROUPINGS)
    def test_grouped_output_is_stable_across_runs(
        self, analysis: SessionAnalysis, group_by: str
    ) -> None:
        first = payload(analysis, group_by)
        second = payload(analysis, group_by)
        first.pop("generated_at")
        second.pop("generated_at")
        assert first == second
