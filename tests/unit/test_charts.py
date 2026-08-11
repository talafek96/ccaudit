"""Contract on the hand-written SVG charts.

A chart is where a breakdown that does not add up is hardest to catch: nobody measures the
pixels. So the geometry itself is the thing pinned here — the segments of a bar sum to the bar,
the children of a node sum to the node, and the unattributed remainder is drawn whenever it is
non-zero rather than tidied away for looking untidy (FR-040).

The payload builder below is shared with ``tests/system/test_report_offline.py``. It builds the
report-data shape by hand, from the component registry rather than from re-typed labels, so
these tests pin the renderer and only the renderer.
"""

import re
from html import escape
from typing import Any

import pytest

from ccaudit.config.components import ATTRIBUTION_COMPONENTS, CHARGE_COMPONENTS, sig_figs_for
from ccaudit.model.reconcile import UNATTRIBUTED_DISPLAY
from ccaudit.money import format_micros, format_share
from ccaudit.render.charts import (
    CHART_WIDTH,
    SERIES_SLOT_COUNT,
    UNATTRIBUTED_SWATCH,
    Slice,
    common_directory_prefix,
    elide_prefix,
    partition,
    row_label,
    series_swatch,
    truncate,
)
from ccaudit.render.charts.bars import (
    CHARACTER_WIDTH,
    COLUMN_GAP,
    ROW_BAR_WIDTH,
    ROW_LABEL_GUTTER,
    ROW_LABEL_LIMIT,
    VALUE_GUTTER,
    VALUE_LABEL_CHARACTERS,
    composition_bar,
    cumulative_sparkline,
    stacked_bars,
)
from ccaudit.render.charts.hierarchy import icicle
from ccaudit.render.charts.scatter import _cause, _money_ticks, cause_scatter, session_bars
from ccaudit.render.charts.timeline import (
    LABEL_GUTTER,
    MAX_SPANS,
    TRACK_WIDTH,
    _runs,
    residency_timeline,
)

TAGS = re.compile(r"<[^>]+>")
SVG_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
RECT = re.compile(r'<rect class="slice[^"]*"[^>]*?width="(\d+)"')
NODE = re.compile(r'<rect class="node(?: [^"]*)?" x="(\d+)" y="(\d+)" width="(\d+)"')

FIXED_TIME = "2026-08-11T12:00:00+00:00"


def slices(*amounts: int, total: int) -> list[Slice]:
    """Categorical slices summing to ``total``, with the remainder last where one is given."""
    return [
        Slice(
            label=f"part {index}",
            micros=amount,
            share=amount / total if total else 0.0,
            sig_figs=2,
            swatch=series_swatch(index),
        )
        for index, amount in enumerate(amounts)
    ]


def report_payload(
    *,
    redact: bool = False,
    unattributed: int = 150_000,
    items: int = 3,
    turns: list[dict[str, Any]] | None = None,
    residency: list[dict[str, Any]] | None = None,
    tree: dict[str, Any] | None = None,
    covered_through_turn: int = 12,
) -> dict[str, Any]:
    """A hand-built report-data payload that reconciles exactly.

    Written out rather than produced by the pipeline so the renderer's contract does not move
    when the model layer does; the arithmetic below is checked by ``assert`` at the end, which
    is the same invariant ``build_report_data`` enforces.
    """
    direct_per_item = 200_000
    carry_per_item = 300_000
    overhead = 400_000
    output = 250_000
    attributed = items * (direct_per_item + carry_per_item) + overhead + output
    total = attributed + unattributed

    item_rows = []
    for index in range(items):
        display = f"redacted-{index:08x}.md" if redact else f"/repo/docs/file{index}.md"
        item_total = direct_per_item + carry_per_item
        row: dict[str, Any] = {
            "item_id": f"file:{display}",
            "kind": "file",
            "display": display,
            "category": "docs",
            "size_tokens": 900 + index,
            "direct_micros": direct_per_item,
            "carry_micros": carry_per_item,
            "total_micros": item_total,
            "share": item_total / total,
            "reads": 2,
            "turns_resident": 11 + index,
            "lanes": {
                "cached_micros": carry_per_item,
                "uncached_micros": 0,
                "loading_micros": direct_per_item,
            },
            "never_cacheable_on": ["claude-opus-4-6"] if index == 0 else [],
            "basis": "measured",
            "confidence": "medium",
            "display_sig_figs": sig_figs_for("medium"),
            "uncertainty": {
                "low_micros": direct_per_item,
                "high_micros": item_total + carry_per_item,
                "driver": "carry_split_policy",
            },
            "per_session": [{"session_id": "session-a", "total_micros": item_total}],
        }
        if not redact:
            row["identity"] = display
        item_rows.append(row)

    concluded = {
        "direct": items * direct_per_item,
        "carry": items * carry_per_item,
        "overhead": overhead,
        "output": output,
    }
    charge = {
        "fresh_input": overhead,
        "cache_write": items * direct_per_item,
        "cache_read": items * carry_per_item + unattributed,
        "output": output,
    }

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": FIXED_TIME,
        "tool_version": "0.0.0",
        "cost_basis": "api_equivalent_estimate",
        "currency": "USD",
        "policy": "proportional",
        # Part of the payload contract (`build_report_data` always emits both). Added here so
        # the renderer can keep reading them strictly: a surface that quietly tolerates a
        # missing key is one that will render a report it cannot describe.
        "group_by": "item",
        "sort_by": "cost",
        "redacted": redact,
        "scope": {
            "sessions_included": ["session-a"],
            "sessions_excluded_count": 0,
            "covered_through_turn": covered_through_turn,
            "provisional": False,
            "producing_versions": ["1.2.3"],
        },
        "totals": {
            "cost_micros": total,
            "attributed_micros": attributed,
            "unattributed_micros": unattributed,
            "attributed_share": attributed / total if total else 0.0,
            "unattributed_share": unattributed / total if total else 0.0,
            "tokens": {
                "fresh_input": 1000,
                "cache_write": 20000,
                "cache_read": 90000,
                "output": 3000,
            },
            "confidence": "high",
            "display_sig_figs": sig_figs_for("high"),
            "uncertainty_notes": [
                "Prices are imputed from published list rates, not billed amounts.",
                "Shared carry cost is divided by the 'proportional' policy.",
            ],
        },
        "components": [
            {
                "id": component.id,
                "technical_name": component.technical_name,
                "plain_name": component.plain_name,
                "description": component.description,
                "tokens": 1000,
                "cost_micros": charge[component.id],
                "share": charge[component.id] / total if total else 0.0,
                "confidence": "high",
                "display_sig_figs": sig_figs_for("high"),
            }
            for component in CHARGE_COMPONENTS
        ],
        "attribution": [
            {
                "id": component.id,
                "technical_name": component.technical_name,
                "plain_name": component.plain_name,
                "description": component.description,
                "per_item": component.id in ("direct", "carry"),
                "cost_micros": concluded[component.id],
                "share": concluded[component.id] / total if total else 0.0,
                "confidence": "medium",
                "display_sig_figs": sig_figs_for("medium"),
            }
            for component in ATTRIBUTION_COMPONENTS
        ],
        "items": item_rows,
        "tree": tree or {},
        "turns": turns or [],
        "residency": residency or [],
        "invalidations": [],
        "comparison": {},
        "diagnostics": {
            "unparseable_records": 2,
            "anchor_reconciliation": [],
            "limitations": ["Some resident instruction content never reaches the transcript."],
            "estimated_figures": 0,
        },
    }
    assert payload["totals"]["attributed_micros"] + unattributed == total
    return payload


def busy_payload(*, turn_count: int = 300, span_count: int = 50) -> dict[str, Any]:
    """A payload the size of a long real session — every section populated.

    Shared with the system test that pins the report's byte budget, so the budget is measured
    against a shape the tool actually produces rather than a toy.
    """
    total = report_payload()["totals"]["cost_micros"]
    per_turn = total // turn_count
    turns = [
        {
            "ordinal": ordinal,
            "cost_micros": per_turn,
            "compaction": {"occurred": ordinal % 90 == 0},
        }
        for ordinal in range(1, turn_count + 1)
    ]
    turns[-1]["cost_micros"] = total - per_turn * (turn_count - 1)

    residency = []
    for index in range(span_count):
        first_turn = 1 + (index * 3) % (turn_count // 4)
        still_resident = index % 5 == 0
        last_turn = None if still_resident else min(turn_count, first_turn + 40 + index)
        end_turn = turn_count if last_turn is None else last_turn
        held = end_turn - first_turn + 1
        residency.append(
            {
                "item_id": f"item-{index}",
                "display": f"/repo/docs/file{index}.md",
                "first_turn": first_turn,
                "last_turn": last_turn,
                "weight_tokens": 900,
                "end_reason": None if still_resident else "evicted",
                "lane_by_turn": ["loading"] + ["cached"] * (held - 1),
            }
        )

    return report_payload(
        tree=sample_tree(total),
        turns=turns,
        residency=residency,
        covered_through_turn=turn_count,
    )


def sample_tree(total: int) -> dict[str, Any]:
    """A folder tree with a remainder node at the root, as the contract specifies."""
    return {
        "name": "/",
        "path": "/",
        "flat_micros": 0,
        "total_micros": total,
        "share": 1.0,
        "children": [
            {
                "name": "repo",
                "path": "/repo",
                "flat_micros": 100_000,
                "total_micros": total - 150_000,
                "share": (total - 150_000) / total,
                "children": [
                    {
                        "name": "docs",
                        "path": "/repo/docs",
                        "flat_micros": total - 250_000,
                        "total_micros": total - 250_000,
                        "share": (total - 250_000) / total,
                        "children": [],
                    }
                ],
            },
            {
                "name": UNATTRIBUTED_SWATCH,
                "path": UNATTRIBUTED_SWATCH,
                "flat_micros": 150_000,
                "total_micros": 150_000,
                "share": 150_000 / total,
                "children": [],
            },
        ],
    }


class TestGeometryAddsUp:
    def test_partition_conserves_the_extent(self) -> None:
        assert sum(partition(720, [1, 7, 13, 0])) == 720

    def test_partition_of_nothing_is_nothing(self) -> None:
        assert partition(0, [3, 4]) == [0, 0]

    def test_a_negative_extent_is_a_broken_invariant(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            partition(-1, [1])

    def test_composition_segments_fill_the_bar_exactly(self) -> None:
        html = composition_bar(
            chart_id="c",
            title="t",
            slices=slices(37, 900_001, 12, total=900_050),
            total_micros=900_050,
        )
        assert sum(int(width) for width in RECT.findall(html)) == 720

    def test_a_composition_that_does_not_add_up_is_refused(self) -> None:
        """The same defect as a table that does not add up, and far harder to spot."""
        with pytest.raises(ValueError, match="do not add up"):
            composition_bar(
                chart_id="c", title="t", slices=slices(10, 20, total=30), total_micros=31
            )

    def test_each_stacked_row_partitions_its_own_bar(self) -> None:
        rows = [
            ("a", slices(700_000, 300_000, total=1_000_000)),
            ("b", slices(1, 999_999, total=1_000_000)),
        ]
        html = stacked_bars(
            chart_id="s",
            title="t",
            rows=rows,
            legend=slices(700_001, 1_299_999, total=2_000_000),
            total_micros=2_000_000,
        )
        widths = [int(width) for width in RECT.findall(html)]
        # Both rows are the same size, so both fill the full track; every segment is accounted
        # for in one row or the other. Read from the layout constant rather than restated: the
        # invariant is that a row's segments partition its bar, not that the bar is 360px, and
        # a test that pins the width fails whenever the label column is widened.
        assert sum(widths) == 2 * ROW_BAR_WIDTH

    def test_icicle_children_partition_their_parent(self) -> None:
        total = 1_000_000
        html = icicle(chart_id="h", title="t", tree=sample_tree(total), total_micros=total)
        nodes = [(int(x), int(y), int(width)) for x, y, width in NODE.findall(html)]
        depth_one = [node for node in nodes if node[1] == 26]
        assert sum(width for _, _, width in depth_one) + _own_width(html, 26) == 720


class TestTheRemainderIsAlwaysDrawn:
    def test_the_unattributed_slice_is_emitted_when_non_zero(self) -> None:
        parts = [
            Slice("work", 900_000, 0.9, 2, series_swatch(0)),
            Slice(UNATTRIBUTED_DISPLAY, 100_000, 0.1, 2, UNATTRIBUTED_SWATCH),
        ]
        html = composition_bar(chart_id="c", title="t", slices=parts, total_micros=1_000_000)
        assert escape(UNATTRIBUTED_DISPLAY) in html
        assert "url(#c-hatch)" in html

    def test_a_zero_remainder_keeps_its_legend_entry(self) -> None:
        """Zero is a finding too — the row stays, it just has no width."""
        parts = [
            Slice("work", 1_000_000, 1.0, 2, series_swatch(0)),
            Slice(UNATTRIBUTED_DISPLAY, 0, 0.0, 2, UNATTRIBUTED_SWATCH),
        ]
        html = composition_bar(chart_id="c", title="t", slices=parts, total_micros=1_000_000)
        assert escape(UNATTRIBUTED_DISPLAY) in html
        assert sum(int(width) for width in RECT.findall(html)) == 720

    def test_the_remainder_node_is_textured_in_the_icicle(self) -> None:
        html = icicle(chart_id="h", title="t", tree=sample_tree(1_000_000), total_micros=1_000_000)
        assert "node--remainder" in html
        assert "url(#h-hatch)" in html


class TestNothingIsInvented:
    def test_an_absent_tree_says_so(self) -> None:
        html = icicle(chart_id="h", title="t", tree={}, total_micros=1_000)
        assert "Not yet available" in html
        assert "<rect" not in html

    def test_absent_residency_says_so(self) -> None:
        html = residency_timeline(chart_id="r", title="t", spans=[], turn_count=10)
        assert "Not yet available" in html

    def test_absent_turns_say_so(self) -> None:
        html = cumulative_sparkline(chart_id="a", title="t", turns=[], total_micros=10, sig_figs=2)
        assert "Not yet available" in html

    def test_a_zero_cost_composition_does_not_crash(self) -> None:
        html = composition_bar(chart_id="c", title="t", slices=[], total_micros=0)
        assert "Not yet available" in html


class TestEveryDistinctionSurvivesGreyscale:
    def test_slices_are_numbered_and_named_in_the_legend(self) -> None:
        html = composition_bar(
            chart_id="c", title="t", slices=slices(60, 40, total=100), total_micros=100
        )
        assert "1. part 0" in html
        assert "2. part 1" in html

    def test_every_mark_carries_a_native_tooltip(self) -> None:
        html = composition_bar(
            chart_id="c", title="t", slices=slices(60, 40, total=100), total_micros=100
        )
        assert html.count("<title>") == 2

    def test_a_ninth_series_is_refused_rather_than_recycled(self) -> None:
        with pytest.raises(ValueError, match="fixed"):
            series_swatch(SERIES_SLOT_COUNT)


class TestResidency:
    def test_a_span_still_resident_runs_to_the_axis_end(self) -> None:
        spans = [
            {
                "item_id": "file:/repo/CLAUDE.md",
                "display": "CLAUDE.md",
                "first_turn": 1,
                "last_turn": None,
                "weight_tokens": 900,
                "end_reason": None,
                "lane_by_turn": ["loading"] + ["cached"] * 9,
            }
        ]
        html = residency_timeline(chart_id="r", title="t", spans=spans, turn_count=10)
        assert "still in context when the session ended" in html
        assert sum(int(width) for width in RECT.findall(html)) == 720 - 210 - 130

    def test_runs_collapse_only_neighbours_in_the_same_lane(self) -> None:
        assert _runs(["a", "a", "b", "a"]) == [("a", 0, 2), ("b", 2, 3), ("a", 3, 4)]
        assert _runs([]) == []

    def test_too_many_spans_says_what_it_left_out(self) -> None:
        """Silent truncation reads as 'this is everything', which would be a lie (FR-040)."""
        spans = [
            {
                "item_id": f"i{index}",
                "display": f"file{index}.md",
                "first_turn": 1,
                "last_turn": 2 + index,
                "lane_by_turn": ["cached"] * (2 + index),
            }
            for index in range(MAX_SPANS + 5)
        ]
        html = residency_timeline(chart_id="r", title="t", spans=spans, turn_count=200)
        assert f"Showing the {MAX_SPANS} longest-resident of {MAX_SPANS + 5} items" in html
        assert "The other 5 are in the table above" in html
        # The longest-resident spans are the ones kept, and the shortest are the ones dropped.
        assert f"file{MAX_SPANS + 4}.md" in html
        assert "file0.md" not in html

    def test_at_the_cap_the_payload_order_is_untouched(self) -> None:
        spans = [
            {
                "item_id": f"i{index}",
                "display": f"file{index}.md",
                "first_turn": 1,
                "last_turn": 2 + index,
                "lane_by_turn": ["cached"] * (2 + index),
            }
            for index in range(MAX_SPANS)
        ]
        html = residency_timeline(chart_id="r", title="t", spans=spans, turn_count=200)
        assert "Showing the" not in html
        labels = re.findall(r'<text class="row-label"[^>]*>([^<]+)</text>', html)
        assert labels == [f"file{index}.md" for index in range(MAX_SPANS)]

    def test_a_backwards_span_is_a_broken_invariant(self) -> None:
        spans = [{"item_id": "x", "display": "x", "first_turn": 5, "last_turn": 2}]
        with pytest.raises(ValueError, match="not a span"):
            residency_timeline(chart_id="r", title="t", spans=spans, turn_count=10)

    def test_runs_draw_the_same_pixels_as_one_rect_per_turn(self) -> None:
        """The run-length collapse is a serialisation change, never a visual one.

        The expected geometry here is the naive drawing — one rectangle per turn, merged where
        neighbours share a lane — so a collapse that moved a boundary, dropped a turn, or
        mislabelled a run fails, while the picture itself is pinned pixel for pixel.
        """
        lanes = ["loading"] + ["cached"] * 40 + ["uncached"] * 5 + ["cached"] * 10
        turn_count = 60
        spans = [
            {
                "item_id": "x",
                "display": "x",
                "first_turn": 3,
                "last_turn": 2 + len(lanes),
                "lane_by_turn": lanes,
                "end_reason": "evicted",
            }
        ]
        html = residency_timeline(chart_id="r", title="t", spans=spans, turn_count=turn_count)

        start_x = LABEL_GUTTER + TRACK_WIDTH * 2 // turn_count
        end_x = LABEL_GUTTER + TRACK_WIDTH * (2 + len(lanes)) // turn_count
        width = max(2, end_x - start_x)
        edges = [start_x + width * position // len(lanes) for position in range(len(lanes) + 1)]
        expected: list[tuple[str, int, int]] = []
        for position, lane in enumerate(lanes):
            piece = (lane, edges[position], edges[position + 1])
            if expected and expected[-1][0] == lane and expected[-1][2] == piece[1]:
                expected[-1] = (lane, expected[-1][1], piece[2])
            else:
                expected.append(piece)
        expected = [piece for piece in expected if piece[2] > piece[1]]

        drawn = [
            (lane, int(x), int(x) + int(w))
            for lane, x, w in re.findall(
                r'<rect class="slice lane lane--(\w+)" x="(\d+)" y="\d+" width="(\d+)"', html
            )
        ]
        assert drawn == expected

    def test_the_full_rate_lane_is_textured_not_only_coloured(self) -> None:
        spans = [
            {
                "item_id": "x",
                "display": "x",
                "first_turn": 1,
                "last_turn": 2,
                "lane_by_turn": ["uncached", "uncached"],
                "end_reason": "evicted",
            }
        ]
        html = residency_timeline(chart_id="r", title="t", spans=spans, turn_count=2)
        assert "url(#r-hatch)" in html


class TestAccumulation:
    def test_compaction_events_are_marked_and_named(self) -> None:
        turns = [
            {"ordinal": 1, "cost_micros": 100, "compaction": {"occurred": False}},
            {"ordinal": 2, "cost_micros": 400, "compaction": {"occurred": True}},
            {"ordinal": 3, "cost_micros": 500, "compaction": {"occurred": False}},
        ]
        html = cumulative_sparkline(
            chart_id="a", title="t", turns=turns, total_micros=1_000, sig_figs=2
        )
        # The event is marked, named, and explained — the invariant. The wording moved: nine
        # copies of "compacted (turn N)" overprinted into a smear, so the label carries the
        # turn, the note carries the word and the count, and the tooltip carries both.
        assert "turn 2" in html
        assert "compaction(s) are marked" in html
        assert "conversation compacted" in html
        assert "spark-event" in html

    def test_turns_that_do_not_reach_the_total_are_refused(self) -> None:
        """The curve ends at 100% by definition; anything else contradicts the total."""
        turns = [{"ordinal": 1, "cost_micros": 100, "compaction": {"occurred": False}}]
        with pytest.raises(ValueError, match="per-turn figures sum to"):
            cumulative_sparkline(chart_id="a", title="t", turns=turns, total_micros=999, sig_figs=2)

    def test_a_single_turn_still_draws(self) -> None:
        turns = [{"ordinal": 1, "cost_micros": 100, "compaction": {"occurred": False}}]
        html = cumulative_sparkline(
            chart_id="a", title="t", turns=turns, total_micros=100, sig_figs=2
        )
        assert "spark-line" in html


def _own_width(html: str, y: int) -> int:
    """The width of the 'own cost' block on a given row, or zero when there is none."""
    pattern = re.compile(rf'<rect class="node node--own" x="\d+" y="{y}" width="(\d+)"')
    return sum(int(width) for width in pattern.findall(html))


class TestLabelsStayIdentifiable:
    """A shortened label that identifies the wrong file is worse than no label.

    Both ends of a path carry identity and neither alone is enough — the tail separates
    siblings, the head says which tree — so the cut goes in the middle, the shared leading
    directories are dropped first because they identify nothing, and the untruncated name is
    always one hover away.
    """

    def test_it_keeps_both_ends(self) -> None:
        assert truncate("/repo/src/model/attribute.py", 20) == "/repo/src…tribute.py"

    def test_two_files_with_the_same_name_stay_distinguishable(self) -> None:
        """The failure a tail-only ellipsis causes: every __init__.py becomes one row."""
        left = truncate("/repo/config/__init__.py", 18)
        right = truncate("/repo/render/__init__.py", 18)
        assert left != right

    def test_a_short_label_is_left_alone(self) -> None:
        assert truncate("skill_listing", 40) == "skill_listing"

    def test_a_gutter_too_narrow_for_two_ends_keeps_one(self) -> None:
        """Below the floor a middle cut leaves two fragments that each identify nothing."""
        assert truncate("/repo/src/model/attribute.py", 6) == "/repo…"

    def test_the_shared_leading_path_is_found(self) -> None:
        shared = common_directory_prefix(
            ["/home/dev/repo/a/x.py", "/home/dev/repo/b/y.py", "/home/dev/repo/b/z.py"]
        )
        assert shared == "/home/dev/repo/"

    def test_a_prefix_shared_by_only_some_labels_is_not_cut(self) -> None:
        assert common_directory_prefix(["/home/dev/a.py", "/var/log/b.py"]) == ""

    def test_a_single_label_has_no_shared_prefix_to_cut(self) -> None:
        assert common_directory_prefix(["/home/dev/repo/a.py"]) == ""

    def test_eliding_marks_the_cut_so_the_path_is_not_read_as_relative(self) -> None:
        assert elide_prefix("/home/dev/repo/a.py", "/home/dev/repo/") == "…/a.py"

    def test_a_label_outside_the_shared_prefix_is_untouched(self) -> None:
        assert elide_prefix("skill_listing", "/home/dev/repo/") == "skill_listing"

    def test_a_truncated_row_label_carries_the_full_name_as_a_tooltip(self) -> None:
        markup = row_label(x=0, y=0, text="/repo/s…/attribute.py", title="/repo/src/attribute.py")
        assert "<title>/repo/src/attribute.py</title>" in markup

    def test_an_untruncated_label_needs_no_tooltip(self) -> None:
        """A tooltip repeating what is already on screen is noise, not help."""
        assert "<title>" not in row_label(x=0, y=0, text="a.py", title="a.py")


class TestCausePlot:
    """The plot that carries the project's claim, so its distortions must be declared.

    A log axis is a real distortion — a step is a multiplication — and one that is not stated is
    a way to make any shape look like any other. These tests pin that it is stated, that the
    ticks carry real values rather than log positions, and that nothing is dropped in silence.
    """

    @pytest.fixture
    def plot(self) -> str:
        return cause_scatter(report_payload()["items"])

    def test_every_priced_item_gets_a_point(self, plot: str) -> None:
        priced = [item for item in report_payload()["items"] if item["total_micros"] > 0]
        assert plot.count('class="point') == len(priced)

    def test_it_says_the_axes_are_logarithmic(self, plot: str) -> None:
        prose = " ".join(TAGS.sub(" ", plot).split())
        assert "logarithmic" in prose
        assert "multiplication rather than an addition" in prose

    def test_the_ticks_carry_real_values_not_log_positions(self, plot: str) -> None:
        """A reader must never have to undo a scale in their head."""
        assert "$" in TAGS.sub(" ", plot)

    def test_no_axis_label_is_repeated(self) -> None:
        """Ticks below a cent all render as '<$0.01'; four identical labels say nothing."""
        labels = _money_ticks(1, 50_000_000)
        rendered = [format_micros(value, 2) for value in labels]
        assert len(rendered) == len(set(rendered))

    def test_each_point_carries_its_figure_and_share(self, plot: str) -> None:
        for title in SVG_TITLE.findall(plot):
            if "—" in title:
                assert "%" in title, title

    def test_the_fill_is_explained_in_words(self, plot: str) -> None:
        """Colour never carries a distinction alone (FR-042)."""
        prose = " ".join(TAGS.sub(" ", plot).split())
        assert "Mostly the keeping" in prose
        assert "Mostly the loading" in prose

    def test_an_item_held_a_long_time_reads_as_the_keeping(self) -> None:
        held = {"total_micros": 100, "carry_micros": 95}
        assert _cause(held)[1] == "mostly the keeping"

    def test_an_item_read_repeatedly_reads_as_the_loading(self) -> None:
        loaded = {"total_micros": 100, "carry_micros": 5}
        assert _cause(loaded)[1] == "mostly the loading"

    def test_a_truncated_plot_says_what_it_left_out(self) -> None:
        items = report_payload()["items"]
        many = [
            dict(item, item_id=f"{item['item_id']}-{index}")
            for index in range(200)
            for item in items
        ]
        prose = " ".join(TAGS.sub(" ", cause_scatter(many)).split())
        assert "not plotted" in prose
        assert "in every total on this page" in prose

    def test_an_empty_selection_is_named_as_missing_rather_than_faked(self) -> None:
        assert "Not yet available" in cause_scatter([])


class TestSessionBars:
    def test_a_single_session_draws_nothing(self) -> None:
        """It would restate the headline as a picture of one bar."""
        assert session_bars([_session_row("only", 100)]) == ""

    def test_each_bar_partitions_its_own_session_total(self) -> None:
        """The three segments are the session total, so the bar reads as a whole."""
        rows = [_session_row("a", 900), _session_row("b", 300)]
        for row in rows:
            assert (
                row["direct_micros"] + row["carry_micros"] + row["other_micros"]
                == (row["cost_micros"])
            )
        assert session_bars(rows)

    def test_every_row_pairs_its_figure_with_a_share(self) -> None:
        chart = session_bars([_session_row("a", 900), _session_row("b", 300)])
        for line in TAGS.sub("\n", chart).splitlines():
            if "$" in line:
                assert "%" in line, line

    def test_the_full_session_id_is_a_hover_away(self) -> None:
        """The label is truncated to eight characters; the id must not be lost."""
        chart = session_bars([_session_row("abcdef0123456789", 900), _session_row("b", 300)])
        assert "abcdef0123456789" in chart


def _session_row(session_id: str, cost: int) -> dict:
    direct = cost // 10
    carry = cost // 2
    return {
        "session_id": session_id,
        "cost_micros": cost,
        "direct_micros": direct,
        "carry_micros": carry,
        "other_micros": cost - direct - carry,
        "turns": 12,
        "share": 0.5,
        "provisional": False,
        "display_sig_figs": 6,
    }


class TestNothingCollides:
    """A bar drawn under its own figure is a chart that cannot be read.

    It happened: the value column was sized by eye at 150 units while the widest figure it has
    to hold is 166, so every clamped bar ran underneath the dollar amount and the broken-axis
    mark was painted over the "$". The geometry below is derived from the font's advance width
    rather than chosen, and these tests are what keep it derived.
    """

    @pytest.mark.parametrize("micros", [1_234_560_000, 99_999_990_000, 1_000, 0])
    def test_the_value_column_holds_the_widest_figure_it_can_be_given(self, micros: int) -> None:
        """Re-derived from the formatter, so widening a figure's format fails here first."""
        widest = f"{format_micros(micros, 6)} ({format_share(1.0)} of total)"
        assert len(widest) <= VALUE_LABEL_CHARACTERS, widest

    def test_a_bar_can_never_reach_the_value_column(self) -> None:
        """Including a clamped one, which is drawn at the full track width."""
        assert ROW_LABEL_GUTTER + ROW_BAR_WIDTH < CHART_WIDTH - VALUE_GUTTER + 1

    def test_a_label_can_never_reach_the_bars(self) -> None:
        assert ROW_LABEL_LIMIT * CHARACTER_WIDTH <= ROW_LABEL_GUTTER - COLUMN_GAP

    def test_every_track_is_wide_enough_to_be_worth_drawing(self) -> None:
        """Sizing the other two columns generously must not squeeze the bars into stubs."""
        assert ROW_BAR_WIDTH >= 200

    def test_the_columns_account_for_the_whole_chart(self) -> None:
        assert ROW_LABEL_GUTTER + ROW_BAR_WIDTH + VALUE_GUTTER == CHART_WIDTH


class TestTooltipsReadLikeSentences:
    def test_a_single_segment_row_is_not_named_twice(self) -> None:
        """ "100 other items — see table — other items — see table: $200" was the real output.

        A row with one segment *is* that segment, so the component name has nothing to add;
        it earns its place only where the bar is actually divided.
        """
        rows = [("100 other items — see table", slices(200, total=200))]
        chart = stacked_bars(
            chart_id="t", title="t", rows=rows, legend=(), total_micros=200, ranked=0
        )
        for title in SVG_TITLE.findall(chart):
            assert title.count("other items") <= 1, title

    def test_a_divided_row_still_names_its_parts(self) -> None:
        """The other half: where a bar has components, the tooltip must say which one."""
        rows = [("spec.md", slices(70, 30, total=100))]
        chart = stacked_bars(
            chart_id="t", title="t", rows=rows, legend=(), total_micros=100, ranked=1
        )
        titles = SVG_TITLE.findall(chart)
        assert any("part 0" in t for t in titles), titles
        assert all(t.startswith("spec.md") for t in titles), titles
