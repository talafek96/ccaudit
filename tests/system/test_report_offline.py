"""The report has to survive leaving the machine.

This is the merge gate for the shareable artifact: it goes to someone who will not install
anything, may open it on a machine with no network at all, and may be disputing the conclusion
it carries. Every test here pins one of the properties that makes it hold up in that room —
no fetch of any kind, every figure paired with its share, the remainder still visible, nothing
worded as a bill, no path when redaction was asked for, and the same bytes on every run.
"""

import json
import math
import re
from html import escape, unescape
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ccaudit.analyse import analyse_transcript
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing
from ccaudit.config.components import ATTRIBUTION_COMPONENTS, CHARGE_COMPONENTS
from ccaudit.model.reconcile import UNATTRIBUTED_DISPLAY
from ccaudit.render.data import build_report_data
from ccaudit.render.report import (
    EXPAND_STEP,
    TOP_ITEMS,
    _comparison_members,
    _parts_details,
    flag,
    forced_reload_micros,
    render_report_html,
    write_report,
)
from tests.fixtures.builder import TranscriptBuilder
from tests.unit.test_charts import busy_payload, report_payload, sample_tree

pytestmark = pytest.mark.system

TAGS = re.compile(r"<[^>]+>")
FIXED_TIME = "2026-08-11T12:00:00+00:00"
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
        assert "turn 2" in html
        assert "compaction(s) are marked" in html
        assert "node--remainder" in html
        assert "still in context when the session ended" in html


class TestItCanBeEmailed:
    """FR-032 is not only about network access — a file nobody can send is not shareable.

    The report went to 3.5 MB the first time the residency section carried real data, because
    the timeline drew one rectangle per item per turn. The budget below is what stops that
    class of regression: it is generous enough that ordinary growth does not trip it, and tight
    enough that a per-mark blow-up (which lands in the megabytes) cannot slip past.
    """

    # 400 KB against ~167 KB for the payload below: ~2.4x headroom for new charts and sections,
    # while a regression to one mark per turn would produce well over a megabyte on this same
    # input. Chosen against the size, not the other way round — if a future section genuinely
    # needs more, raise it deliberately and say why here.
    BUDGET_BYTES = 400 * 1024

    def test_a_long_session_stays_under_the_budget(self) -> None:
        html = render_report_html(busy_payload(turn_count=300, span_count=50))
        assert len(html.encode("utf-8")) < self.BUDGET_BYTES, (
            f"report grew to {len(html.encode('utf-8')):,} bytes, over the "
            f"{self.BUDGET_BYTES:,} byte budget"
        )

    def test_the_timeline_draws_runs_rather_than_one_mark_per_turn(self) -> None:
        """The marks are bounded by the runs in the data, not by turns times spans."""
        html = render_report_html(busy_payload(turn_count=300, span_count=50))
        # Two lanes per span at most in this fixture, plus the hit target, plus every other
        # chart on the page. One mark per turn would be several thousand.
        assert html.count("<rect") < 400

    def test_nothing_is_dropped_silently_when_the_chart_is_capped(self) -> None:
        html = render_report_html(busy_payload(turn_count=300, span_count=90))
        assert "Showing the 60 longest-resident of 90 items" in html
        assert "The other 30 are in the table above" in html


class TestWriting:
    def test_write_report_creates_the_file_and_its_parent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "report.html"
        written = write_report(report_payload(), target)
        assert written == target
        assert target.read_text(encoding="utf-8").startswith("<!doctype html>")


def expansion_states(html: str) -> list[dict]:
    """The rendered expansion states, as the script will read them."""
    match = re.search(r'data-expand-states="([^"]*)"', html)
    assert match, "the truncation line carries no expansion states"
    return json.loads(unescape(match.group(1)))


class TestProgressiveReveal:
    """The truncated tail is expandable, and the table still adds up at every step.

    Twelve rows and a "70 other items" line is unreadable when that line is 45% of the spend:
    the reader can see that most of the money is off-screen and has no way to look at it. So
    the rest of the rows ship in the file, hidden, and a button reveals them in batches.

    The hazard this creates is the one that matters most here. Revealing a row has to shrink
    the remainder line by exactly that row's cost, or the table stops reconciling the moment
    someone clicks — a show-stopper (Principle X, invariant A1). It cannot drift, because the
    script never computes a figure: every state was rendered in Python and the script only
    swaps between them.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def many_items(cls) -> str:
        builder = TranscriptBuilder()
        builder.add_user_text("read the tree")
        for index in range(40):
            tool_id = f"t{index}"
            builder.add_turn(
                input_tokens=5,
                cache_creation_5m=4_000,
                cache_read=4_000 * index,
                output_tokens=30,
                tool_use_ids=(tool_id,),
            )
            builder.add_tool_result(
                tool_use_id=tool_id,
                file_path=f"/repo/pkg{index % 7}/module_{index}.py",
                text="x = 1\n" * (300 + index * 20),
            )
        with TemporaryDirectory() as tmp:
            path = builder.write(Path(tmp) / "s.jsonl")
            analysis = analyse_transcript(path, pricing=load_pricing(BUNDLED_PRICING_PATH))
        return render_report_html(build_report_data([analysis], generated_at=FIXED_TIME))

    def test_the_hidden_rows_are_in_the_file(self, many_items: str) -> None:
        """Revealing a row is a display change. Nothing is fetched and nothing is recomputed."""
        assert many_items.count('data-overflow="1"') > 0

    def test_they_start_hidden_so_the_static_page_is_the_ranked_summary(
        self, many_items: str
    ) -> None:
        for row in re.findall(r"<tr[^>]*data-overflow[^>]*>", many_items):
            assert "hidden" in row, row

    def test_there_is_a_control_to_reveal_them(self, many_items: str) -> None:
        assert "expand-btn" in many_items
        assert "Show " in many_items

    def test_the_control_is_hidden_without_javascript(self, many_items: str) -> None:
        """The page must stay complete and honest in a browser with scripting off (FR-032)."""
        button = re.search(r'<button[^>]*class="expand-btn[^"]*"', many_items)
        assert button and "js-only" in button.group(0)

    def test_every_expansion_state_reconciles(self, many_items: str) -> None:
        """The invariant the feature could break: each state's figure covers exactly the rows
        still hidden at that point."""
        states = expansion_states(many_items)
        rows = re.findall(r'<tr[^>]*data-overflow="1"[^>]*data-total="(\d+)"', many_items)
        hidden_micros = [int(value) for value in rows]
        assert len(states) == math.ceil(len(hidden_micros) / EXPAND_STEP)
        for index, state in enumerate(states):
            revealed = index * EXPAND_STEP
            assert state["count"] == len(hidden_micros) - revealed
            assert state["micros"] == sum(hidden_micros[revealed:])

    def test_the_first_state_matches_the_line_the_reader_sees(self, many_items: str) -> None:
        """The rendered line and state zero are the same claim; they must not disagree."""
        states = expansion_states(many_items)
        assert states[0]["label"] in TAGS.sub(" ", many_items)

    def test_every_state_figure_is_still_paired_with_its_share(self, many_items: str) -> None:
        """The honesty rule does not lapse because a figure is delivered by a script."""
        states = expansion_states(many_items)
        for state in states:
            assert MONEY_THEN_SHARE.search(state["figure"]), state


class TestTheRenderedTableAddsUp:
    """The caption's claim, checked against the rendered rows rather than against the payload.

    The payload reconciling is not the same property as the *table* reconciling, and the gap
    between them is where cost goes missing: forced-reload cost is attributed to an
    invalidation event rather than to a file, so it appears in `attributed_micros` but has no
    item row. Before this test the table was silently short by exactly that amount while every
    payload-level check passed.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def invalidating(cls) -> tuple[dict, str]:
        builder = TranscriptBuilder()
        builder.add_user_text("read it")
        builder.add_turn(
            model="claude-opus-5",
            input_tokens=10,
            cache_creation_5m=9_000,
            output_tokens=40,
            tool_use_ids=("t1",),
        )
        builder.add_tool_result(
            tool_use_id="t1", file_path="/repo/src/app.py", text="x = 1\n" * 400
        )
        builder.add_turn(model="claude-opus-5", input_tokens=5, cache_read=9_000, output_tokens=30)
        # Past TOP_ITEMS, so the truncation line and the hidden rows are in play too: both
        # carry a total, and only one of them may be counted at a time.
        for index in range(TOP_ITEMS + 4):
            tool_id = f"x{index}"
            builder.add_turn(
                model="claude-opus-5",
                input_tokens=5,
                cache_creation_5m=3_000,
                cache_read=9_000,
                output_tokens=20,
                tool_use_ids=(tool_id,),
            )
            builder.add_tool_result(
                tool_use_id=tool_id,
                file_path=f"/repo/pkg/mod_{index}.py",
                text="y = 2\n" * (120 + index * 10),
            )
        # Switching model invalidates the whole prefix and forces a full re-write, which is
        # charged to the switch rather than to the file that happened to be resident (FR-081).
        builder.add_turn(
            model="claude-sonnet-5", input_tokens=5, cache_creation_5m=14_000, output_tokens=30
        )
        builder.add_turn(
            model="claude-sonnet-5", input_tokens=5, cache_read=14_000, output_tokens=30
        )
        with TemporaryDirectory() as tmp:
            path = builder.write(Path(tmp) / "s.jsonl")
            analysis = analyse_transcript(path, pricing=load_pricing(BUNDLED_PRICING_PATH))
        payload = build_report_data([analysis], generated_at=FIXED_TIME)
        return payload, render_report_html(payload)

    def test_the_fixture_actually_forces_a_reload(self, invalidating: tuple[dict, str]) -> None:
        """Guard on the guard: a fixture with no invalidation would pass everything vacuously."""
        payload, _ = invalidating
        assert forced_reload_micros(payload) > 0

    def test_every_row_in_the_table_sums_to_the_session_total(
        self, invalidating: tuple[dict, str]
    ) -> None:
        payload, html = invalidating
        # Hidden overflow rows are excluded: the truncation line stands in for them, and
        # counting both would double the tail. That swap is exactly what the reveal performs.
        rows = [
            attributes
            for attributes in re.findall(r"<tr([^>]*)>", html)
            if "data-total=" in attributes and "data-overflow" not in attributes
        ]
        assert rows, "no rows carried a machine-readable total"
        totals = [int(match) for row in rows for match in re.findall(r'data-total="(-?\d+)"', row)]
        assert len(totals) == len(rows)
        assert sum(totals) == payload["totals"]["cost_micros"]

    def test_the_fixture_actually_truncates(self, invalidating: tuple[dict, str]) -> None:
        """The other guard: without a truncation line the double-count case is never exercised."""
        _, html = invalidating
        assert 'data-overflow="1"' in html

    def test_the_forced_reload_has_its_own_named_row(self, invalidating: tuple[dict, str]) -> None:
        """Not folded into a file's figure: the finding is what the *change* cost (FR-081)."""
        _, html = invalidating
        assert "Re-loading after a change (invalidation)" in html

    def test_a_payload_without_invalidations_grows_no_such_row(self, html: str) -> None:
        assert "Re-loading after a change" not in html


class TestABarOpensToWhatIsInside:
    """A label is not evidence.

    "Instruction files — $6.68" beside an $86 "Skills" bar reads as *CLAUDE.md is not counted*,
    and a reader given only the label has no way to find out that it is. Likewise a composite
    item drawn as one row ("skill_listing") hides the 25 skills whose costs it is the sum of.
    Both must open to their members, on the surface where the figure appears.
    """

    def test_a_composite_item_opens_to_its_parts(self) -> None:
        item = {
            "display_sig_figs": 6,
            "parts": [
                {
                    "name": "dataviz",
                    "cost_micros": 300_000,
                    "share_of_item": 0.6,
                    "origin": "plugin",
                    "plugin": "claude-plugins-official",
                },
                {
                    "name": "loop",
                    "cost_micros": 200_000,
                    "share_of_item": 0.4,
                    "origin": "not stated",
                    "plugin": "",
                },
            ],
        }
        details = _parts_details(item)
        assert details.startswith("<details")
        assert "dataviz" in details and "loop" in details

    def test_an_item_with_no_parts_grows_no_empty_control(self) -> None:
        """A control that opens onto nothing is worse than no control."""
        assert _parts_details({"display_sig_figs": 6, "parts": []}) == ""

    def test_a_comparison_bar_opens_to_the_items_behind_it(self) -> None:
        comparison = {
            "resident_instruction": [
                {
                    "label": "Instruction files",
                    "cost_micros": 500_000,
                    "members": [
                        {"name": "/repo/CLAUDE.md", "cost_micros": 300_000},
                        {"name": "/home/.claude/CLAUDE.md", "cost_micros": 200_000},
                    ],
                }
            ]
        }
        data = {"totals": {"display_sig_figs": 6, "cost_micros": 1_000_000}}
        members = _comparison_members(comparison, data)
        assert "/repo/CLAUDE.md" in members
        assert "/home/.claude/CLAUDE.md" in members

    def test_a_bar_with_no_members_grows_no_empty_control(self) -> None:
        comparison = {"resident_instruction": [{"label": "Skills", "cost_micros": 0}]}
        data = {"totals": {"display_sig_figs": 6, "cost_micros": 1_000_000}}
        assert _comparison_members(comparison, data) == ""

    def test_the_members_are_ordered_by_cost_so_the_dearest_is_first(self) -> None:
        comparison = {
            "resident_instruction": [
                {
                    "label": "Instruction files",
                    "cost_micros": 500_000,
                    "members": [
                        {"name": "dear.md", "cost_micros": 400_000},
                        {"name": "cheap.md", "cost_micros": 100_000},
                    ],
                }
            ]
        }
        data = {"totals": {"display_sig_figs": 6, "cost_micros": 1_000_000}}
        members = _comparison_members(comparison, data)
        assert members.index("dear.md") < members.index("cheap.md")


class TestEveryTagExplainsItself:
    """A tag is a compression a reader cannot expand unless the page expands it.

    "too small to cache on claude-opus-5" is four facts in six words, and "(mixed)" is a
    category that is not one. Jargon only the author understands is a defect (Principle X), so
    every tag carries its sentence, from one registry, on the element itself.
    """

    def test_every_tag_carries_its_explanation(self, html: str) -> None:
        tags = re.findall(r'<span class="flag" data-tag="([^"]+)" data-tip="([^"]*)"', html)
        assert tags
        for tag, tip in tags:
            assert len(tip) > 30, f"{tag} has no real explanation"

    def test_a_tag_explains_itself_without_javascript_too(self, html: str) -> None:
        """`data-tip` is read by the page's own balloon; `title` is what is left without it."""
        for match in re.finditer(
            r'<span class="flag" data-tag="[^"]+" data-tip="([^"]*)" title="([^"]*)"', html
        ):
            assert match.group(1) == match.group(2)

    def test_an_unknown_tag_is_refused_rather_than_shown_bare(self) -> None:
        """A tag with no sentence is the exact defect this registry exists to prevent."""
        with pytest.raises(KeyError, match="unknown tag"):
            flag("whatever", "not-a-real-tag")

    def test_the_uncacheable_tag_names_the_model_it_is_about(self) -> None:
        rendered = flag("too small to cache on m", "uncacheable", model="claude-opus-5")
        assert "claude-opus-5" in rendered

    def test_a_row_carries_its_tags_for_filtering(self, html: str) -> None:
        """Clicking a tag filters by it, which needs the row to say which tags it has."""
        rows = re.findall(r'data-tags="([^"]*)"', html)
        assert rows
        assert any(row for row in rows), "no row carries a tag"
