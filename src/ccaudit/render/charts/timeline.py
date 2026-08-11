"""The residency timeline — one bar per span, so prolonged residency is visible (FR-036).

This is the chart the tool exists for. A file read once looks cheap in a leaderboard sorted by
reads; the same file sitting in context for ninety turns is charged every one of them, and only
a timeline makes that length legible at a glance rather than as a number in a column.

The x axis is turns, not wall-clock: cost accrues per turn, and an hour spent thinking between
two turns costs nothing. Within a span, each turn is drawn in its lane — loading, kept at the
cache rate, or kept at full rate because the content is below the model's minimum cacheable
size. That last lane is the finding a reader must not have to go looking for (FR-078), so it
carries a texture as well as a colour.

Spans that were still resident when the session ended are drawn to the axis end and labelled;
they are not extended past it, and not guessed at.
"""

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

from ccaudit.render.charts import (
    CHART_WIDTH,
    LABEL_GUTTER,
    ROW_HEIGHT,
    UNATTRIBUTED_SWATCH,
    figure,
    hatch_defs,
    placeholder,
    row_label,
    svg_open,
    swatch_fill,
    tick_label,
    truncate,
)

SPAN_HEIGHT = 14
END_GUTTER = 130
TRACK_WIDTH = CHART_WIDTH - LABEL_GUTTER - END_GUTTER
LABEL_LIMIT = 30
AXIS_HEIGHT = 22

# Lane names come from the payload's ``lane_by_turn``; each maps to a swatch role and a
# plain-language sentence, because "uncached" means nothing to the reader this report is for.
LANE_SWATCH = {
    "loading": "series-1",
    "cached": "series-2",
    "uncached": "series-8",
}
LANE_MEANING = {
    "loading": "being loaded into context",
    "cached": "kept in context at the cache rate",
    "uncached": "kept in context at full rate — too small to cache",
}
UNCACHED_LANE = "uncached"

END_REASON_TEXT = {
    "evicted": "dropped when the conversation was compacted",
    "invalidated": "reloaded because something before it changed",
    "session_end": "still in context when the session ended",
    "unknown": "stopped being charged for a reason the records do not state",
}


def residency_timeline(
    *,
    chart_id: str,
    title: str,
    spans: Sequence[Mapping[str, Any]],
    turn_count: int,
    note: str = "",
) -> str:
    """Draw one bar per residency span across a shared turn axis."""
    if not spans:
        return placeholder(
            chart_id=chart_id,
            title=title,
            reason=(
                "Per-turn residency spans are not yet computed, so how long each item stayed in "
                "context cannot be drawn."
            ),
        )
    if turn_count <= 0:
        raise ValueError(f"a residency timeline needs at least one turn, got {turn_count}")

    body: list[str] = []
    for index, span in enumerate(spans):
        body.append(_span_row(span=span, chart_id=chart_id, index=index, turn_count=turn_count))

    height = ROW_HEIGHT * len(spans) + AXIS_HEIGHT
    axis_y = ROW_HEIGHT * len(spans) + 6
    axis = "".join(
        [
            (
                f'<line class="axis" x1="{LABEL_GUTTER}" y1="{axis_y}" '
                f'x2="{LABEL_GUTTER + TRACK_WIDTH}" y2="{axis_y}"></line>'
            ),
            tick_label(x=LABEL_GUTTER, y=axis_y + 14, text="turn 1", anchor="start"),
            tick_label(
                x=LABEL_GUTTER + TRACK_WIDTH,
                y=axis_y + 14,
                text=f"turn {turn_count}",
                anchor="end",
            ),
        ]
    )

    svg = "".join(
        [
            svg_open(
                CHART_WIDTH,
                height,
                label=f"{title}. One bar per item, spanning the turns it stayed in context.",
            ),
            hatch_defs(chart_id),
            "".join(body),
            axis,
            "</svg>",
        ]
    )
    legend = _lane_legend()
    return figure(chart_id=chart_id, title=title, svg=svg, legend=legend, note=note)


def _span_row(*, span: Mapping[str, Any], chart_id: str, index: int, turn_count: int) -> str:
    """One item's bar, divided into its per-turn lanes.

    The lane segments partition the span exactly, so the bar's length is the residency and its
    composition is what that residency was charged at.
    """
    first_turn = int(span["first_turn"])
    last_turn = span.get("last_turn")
    end_turn = turn_count if last_turn is None else int(last_turn)
    if first_turn < 1 or end_turn < first_turn:
        raise ValueError(
            f"residency span for {span.get('item_id')!r} runs from turn {first_turn} to "
            f"{end_turn}, which is not a span"
        )

    y = index * ROW_HEIGHT
    text_y = y + SPAN_HEIGHT + 1
    start_x = LABEL_GUTTER + TRACK_WIDTH * (first_turn - 1) // turn_count
    end_x = LABEL_GUTTER + TRACK_WIDTH * end_turn // turn_count
    width = max(2, end_x - start_x)

    lanes: Sequence[str] = span.get("lane_by_turn") or ()
    parts: list[str] = []
    if lanes:
        # Equal-width turns within the span: a turn is the unit the charge is levied on, so
        # every turn gets the same slice of the bar regardless of what it cost.
        edges = [start_x + width * position // len(lanes) for position in range(len(lanes) + 1)]
        for position, lane in enumerate(lanes):
            lane_width = edges[position + 1] - edges[position]
            if lane_width <= 0:
                continue
            parts.append(
                f'<g class="mark"><rect class="slice lane lane--{escape(str(lane))}" '
                f'x="{edges[position]}" y="{y}" width="{lane_width}" height="{SPAN_HEIGHT}" '
                f'fill="{_lane_fill(chart_id, str(lane))}"></rect>'
                f"<title>Turn {first_turn + position}: "
                f"{escape(LANE_MEANING.get(str(lane), str(lane)))}</title></g>"
            )
    else:
        parts.append(
            f'<g class="mark"><rect class="slice lane lane--cached" x="{start_x}" y="{y}" '
            f'width="{width}" height="{SPAN_HEIGHT}" fill="var(--series-2)"></rect>'
            f"<title>Resident from turn {first_turn} to turn {end_turn}</title></g>"
        )

    display = str(span.get("display", span.get("item_id", "")))
    reason_key = span.get("end_reason") or ("session_end" if last_turn is None else "unknown")
    reason = END_REASON_TEXT.get(str(reason_key), str(reason_key))
    turns_held = end_turn - first_turn + 1
    return "".join(
        [
            row_label(x=LABEL_GUTTER - 10, y=text_y, text=truncate(display, LABEL_LIMIT)),
            "".join(parts),
            (
                f'<text class="row-value" x="{CHART_WIDTH}" y="{text_y}" text-anchor="end">'
                f"{turns_held} turns</text>"
            ),
            (
                f'<g class="mark"><rect class="hit" x="{LABEL_GUTTER}" y="{y}" '
                f'width="{TRACK_WIDTH}" height="{SPAN_HEIGHT}" fill="transparent"></rect>'
                f"<title>{escape(display)}: turns {first_turn}–{end_turn} "
                f"({turns_held} turns) — {escape(reason)}</title></g>"
            ),
        ]
    )


def _lane_fill(chart_id: str, lane: str) -> str:
    """The paint for a lane; the full-rate lane also carries a texture (FR-042, FR-078)."""
    if lane == UNCACHED_LANE:
        return swatch_fill(chart_id, UNATTRIBUTED_SWATCH)
    return f"var(--{LANE_SWATCH.get(lane, 'series-2')})"


def _lane_legend() -> str:
    """A legend in plain language — the lane names alone mean nothing to the reader."""
    items = "".join(
        f'<li class="legend-item">'
        f'<span class="swatch swatch--{LANE_SWATCH[lane]}" aria-hidden="true">{index}</span>'
        f'<span class="legend-label">{index}. {escape(lane)} — {escape(LANE_MEANING[lane])}'
        f"</span></li>"
        for index, lane in enumerate(LANE_SWATCH, start=1)
    )
    return f'<ol class="legend">{items}</ol>'
