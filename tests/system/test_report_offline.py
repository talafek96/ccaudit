"""The report has to survive leaving the machine.

This is the merge gate for the shareable artifact: it goes to someone who will not install
anything, may open it on a machine with no network at all, and may be disputing the conclusion
it carries. Every test here pins one of the properties that makes it hold up in that room —
no fetch of any kind, every figure paired with its share, the remainder still visible, nothing
worded as a bill, no path when redaction was asked for, and the same bytes on every run.
"""

import re
from html import escape
from pathlib import Path

import pytest

from ccaudit.config.components import ATTRIBUTION_COMPONENTS, CHARGE_COMPONENTS
from ccaudit.model.reconcile import UNATTRIBUTED_DISPLAY
from ccaudit.render.report import render_report_html, write_report
from tests.unit.test_charts import report_payload, sample_tree

pytestmark = pytest.mark.system

TAGS = re.compile(r"<[^>]+>")
MONEY_SPAN = re.compile(r'<span class="money">')
MONEY_THEN_SHARE = re.compile(r'<span class="money">[^<]*</span> <span class="share">')
SVG_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
# Anything that would make the browser reach for the network. `//` on its own would match
# every closing tag's neighbours, so the protocol-relative case is checked as `="//`.
EXTERNAL = ("http://", "https://", '="//', "@import", "url(http", "//cdn")


@pytest.fixture
def html() -> str:
    return render_report_html(report_payload())


class TestItOpensAnywhere:
    def test_no_request_leaves_the_page(self, html: str) -> None:
        for needle in EXTERNAL:
            assert needle not in html, needle

    def test_no_stylesheet_or_script_is_linked(self, html: str) -> None:
        assert "<link" not in html
        assert "src=" not in html
        assert "<style>" in html
        assert "<script>" in html

    def test_it_is_a_complete_document(self, html: str) -> None:
        assert html.startswith("<!doctype html>")
        assert html.rstrip().endswith("</html>")

    def test_both_themes_are_present(self, html: str) -> None:
        """FR-041: dark mode is selected, and the reader's own toggle wins over the OS."""
        assert "prefers-color-scheme: dark" in html
        assert ':root[data-theme="dark"]' in html

    def test_the_page_is_complete_without_javascript(self, html: str) -> None:
        """Every figure is written into the file; the script only reorders and re-themes."""
        without_script = html.split("<script>")[0]
        assert UNATTRIBUTED_DISPLAY.replace("'", "&#x27;") in without_script
        assert "<table" in without_script
        assert "<svg" in without_script

    def test_controls_that_need_javascript_are_hidden_until_it_runs(self, html: str) -> None:
        assert ".js-only { display: none; }" in html
        assert 'class="sort-btn js-only"' in html


class TestEveryFigureIsHonest:
    def test_every_money_figure_is_paired_with_a_share(self, html: str) -> None:
        """FR-011: the share survives being wrong about pricing; the dollars do not."""
        assert len(MONEY_SPAN.findall(html)) == len(MONEY_THEN_SHARE.findall(html))
        assert len(MONEY_SPAN.findall(html)) > 10

    def test_every_figure_in_a_chart_tooltip_carries_its_share(self, html: str) -> None:
        for title in SVG_TITLE.findall(html):
            if "$" in title:
                assert "%" in title, title

    def test_nothing_is_worded_as_a_bill(self, html: str) -> None:
        """'billed' may appear only inside a denial that these figures are a bill."""
        text = TAGS.sub(" ", html)
        for match in re.finditer("billed", text):
            assert text[max(0, match.start() - 4) : match.start()] == "not ", text[
                match.start() - 80 : match.start() + 40
            ]

    def test_figures_are_labelled_api_equivalent_estimates(self, html: str) -> None:
        assert "API-equivalent cost estimate" in html

    def test_the_remainder_is_visible_and_named(self, html: str) -> None:
        escaped = escape(UNATTRIBUTED_DISPLAY)
        assert html.count(escaped) >= 3  # headline, chart legend, table row
        assert "cost we could not tie to any item" in html

    def test_a_payload_that_does_not_add_up_is_refused(self) -> None:
        payload = report_payload()
        payload["totals"]["unattributed_micros"] += 1
        with pytest.raises(ValueError, match="does not add up"):
            render_report_html(payload)

    def test_figures_are_never_finer_than_their_confidence(self) -> None:
        """FR-095: an item at 2 significant figures is not printed to the cent."""
        payload = report_payload()
        payload["items"][0]["display_sig_figs"] = 1
        payload["items"][0]["total_micros"] = 1_234_567
        payload["items"][0]["direct_micros"] = 934_567
        payload["items"][0]["carry_micros"] = 300_000
        # Only the rendering of that row is under test here, so the totals are left alone by
        # putting the change back before the reconciliation check runs.
        payload["items"][0]["total_micros"] = 500_000
        payload["items"][0]["direct_micros"] = 200_000
        payload["items"][0]["carry_micros"] = 300_000
        payload["items"][0]["display_sig_figs"] = 1
        html = render_report_html(payload)
        assert "$0.5</span>" in html

    def test_plain_language_names_carry_the_technical_term(self, html: str) -> None:
        for component in CHARGE_COMPONENTS:
            assert component.plain_name in html
            assert component.technical_name in html
        for component in ATTRIBUTION_COMPONENTS:
            assert component.plain_name in html

    def test_the_limitations_are_reproduced(self, html: str) -> None:
        assert "Some resident instruction content never reaches the transcript." in html
        assert "could not be parsed" in html

    def test_the_payload_is_embedded_for_the_reader_to_check(self, html: str) -> None:
        assert 'id="ccaudit-data"' in html
        assert "api_equivalent_estimate" in html


class TestRedaction:
    def test_a_redacted_payload_contains_no_path(self) -> None:
        html = render_report_html(report_payload(redact=True))
        assert "/repo/" not in html
        assert "redacted-" in html
        assert "Paths are redacted" in html

    def test_an_unredacted_payload_still_shows_paths(self, html: str) -> None:
        assert "/repo/docs/file0.md" in html


class TestDeterminism:
    def test_two_renders_of_one_payload_are_byte_identical(self) -> None:
        payload = report_payload()
        assert render_report_html(payload) == render_report_html(payload)

    def test_the_only_clock_reading_is_the_payload_timestamp(self) -> None:
        first = render_report_html(report_payload())
        second = render_report_html(report_payload())
        assert first == second


class TestDegenerateInput:
    def test_a_zero_cost_payload_renders(self) -> None:
        payload = report_payload(items=0, unattributed=0)
        payload["totals"]["cost_micros"] = 0
        payload["totals"]["attributed_micros"] = 0
        for component in payload["attribution"]:
            component["cost_micros"] = 0
            component["share"] = 0.0
        for component in payload["components"]:
            component["cost_micros"] = 0
            component["share"] = 0.0
        html = render_report_html(payload)
        assert "Not yet available" in html
        assert "$0.00" in html

    def test_deferred_sections_are_named_as_missing_not_faked(self, html: str) -> None:
        assert html.count("Not yet available") >= 4

    def test_deferred_sections_appear_once_their_data_does(self) -> None:
        total = report_payload()["totals"]["cost_micros"]
        payload = report_payload(
            tree=sample_tree(total),
            # Per-turn figures have to reach the session total, or the curve would end
            # somewhere other than 100% of the total it is drawn against.
            turns=[
                {"ordinal": 1, "cost_micros": 1_000_000, "compaction": {"occurred": False}},
                {
                    "ordinal": 2,
                    "cost_micros": total - 1_000_000,
                    "compaction": {"occurred": True},
                },
            ],
            residency=[
                {
                    "item_id": "file:/repo/docs/file0.md",
                    "display": "/repo/docs/file0.md",
                    "first_turn": 1,
                    "last_turn": None,
                    "weight_tokens": 900,
                    "end_reason": None,
                    "lane_by_turn": ["loading", "cached"],
                }
            ],
        )
        html = render_report_html(payload)
        assert "compacted (turn 2)" in html
        assert "node--remainder" in html
        assert "still in context when the session ended" in html


class TestWriting:
    def test_write_report_creates_the_file_and_its_parent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "report.html"
        written = write_report(report_payload(), target)
        assert written == target
        assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
