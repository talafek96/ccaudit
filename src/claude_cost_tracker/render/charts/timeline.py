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

**Runs, not turns.** A span's ``lane_by_turn`` is overwhelmingly contiguous — an item sits in
the cached lane for two hundred turns, it does not alternate — so consecutive turns in the same
lane are emitted as *one* rectangle covering exactly the same pixels the individual turns
covered. This is a serialisation change and not a visual one: the run's edges are the first
turn's left edge and the last turn's right edge, both computed by the identical formula. On a
361-turn session with 67 spans it is the difference between a 3.5 MB report and a 300 KB one,
and a report nobody can email is a report that failed FR-032 however correct its figures are.
"""

import math
from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

from claude_cost_tracker.ingest.discover import SHORT_ID_LENGTH
from claude_cost_tracker.render.charts import (
    CHART_WIDTH,
    LABEL_GUTTER,
    ROW_HEIGHT,
    UNATTRIBUTED_SWATCH,
    common_directory_prefix,
    elide_prefix,
    figure,
    gridlines,
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

# Past this many rows the chart is taller than any screen and the bars are too thin to compare,
# so drawing more of them adds no information. When the cap bites, the longest-resident spans
# are the ones kept — length is the question this chart exists to answer — and the omission is
# stated on the face of the chart. Every omitted span is still in the embedded payload and in
# the tables, so the cap costs the reader nothing but a scroll.
MAX_SPANS = 60
OMISSION_HEIGHT = 20

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

    drawn, omitted = _select(spans, turn_count)
    # Stated in the drawing itself, not only in the caption: a truncated chart that does not say
    # so reads as "this is everything", which is the one thing it must never imply.
    omission = (
        ""
        if not omitted
        else (
            f"Showing the {len(drawn)} longest-resident of {len(spans)} items. "
            f"The other {omitted} are in the table above and in this file's embedded data."
        )
    )
    offset = OMISSION_HEIGHT if omission else 0

    body: list[str] = []
    if omission:
        body.append(
            f'<text class="omission" x="0" y="12" text-anchor="start">{escape(omission)}</text>'
        )
    # One prefix for the whole chart, so the labels stay comparable down the column.
    shared = common_directory_prefix(
        [str(span.get("display", span.get("item_id", ""))) for span in drawn]
    )
    # One row per *item*, not per span. A span belongs to one session, so a selection holding
    # twenty-six sessions produced twenty-six identically labelled `skill_listing` rows —
    # correct, and unreadable. They are the same item, so they share a row, and the periods it
    # was resident in each session are drawn as separate bars along it. Nothing is merged
    # arithmetically: each bar is still exactly the span that was observed.
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for span in drawn:
        rows.setdefault(str(span.get("display", span.get("item_id", ""))), []).append(span)
    for index, (_label, group) in enumerate(rows.items()):
        for span in group:
            body.append(
                _span_row(
                    span=span,
                    chart_id=chart_id,
                    index=index,
                    turn_count=turn_count,
                    top=offset,
                    shared_prefix=shared,
                    # Only the first bar on a row carries the label; the rest are the same item
                    # in another session and a repeated label would be noise.
                    draw_label=span is group[0],
                    spans_in_row=len(group),
                )
            )

    # Rows, not spans: several spans of one item share a row.
    height = ROW_HEIGHT * len(rows) + AXIS_HEIGHT + offset
    axis_y = ROW_HEIGHT * len(rows) + 6 + offset
    # Turn markers across the tracks. "This file was resident from turn 1 to turn 1612" is
    # only legible against a ruled axis; with the two end labels alone, a bar's start and end
    # can be seen but not read.
    ruled = _turn_ticks(turn_count)
    axis = "".join(
        [
            gridlines(
                [LABEL_GUTTER + TRACK_WIDTH * turn // turn_count for turn in ruled],
                span=(offset, axis_y),
            ),
            (
                f'<line class="axis" x1="{LABEL_GUTTER}" y1="{axis_y}" '
                f'x2="{LABEL_GUTTER + TRACK_WIDTH}" y2="{axis_y}"></line>'
            ),
            tick_label(x=LABEL_GUTTER, y=axis_y + 14, text="turn 1", anchor="start"),
            "".join(
                tick_label(
                    x=LABEL_GUTTER + TRACK_WIDTH * turn // turn_count,
                    y=axis_y + 14,
                    text=f"{turn:,}",
                )
                for turn in ruled
            ),
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
    caption = f"{omission} {note}".strip() if omission else note
    return figure(chart_id=chart_id, title=title, svg=svg, legend=legend, note=caption)


def _turn_ticks(turn_count: int) -> list[int]:
    """Round turn numbers to rule the track with, excluding the ends.

    The ends already carry "turn 1" and "turn N"; a tick beside either would overprint it. The
    step is a round number a reader would say out loud rather than an arithmetic fraction of
    however many turns this session happened to run to.
    """
    if turn_count < 8:
        return []
    rough = turn_count / 5
    magnitude = 10 ** math.floor(math.log10(rough))
    for multiple in (1, 2, 2.5, 5, 10):
        step = int(magnitude * multiple)
        if step >= rough:
            break
    # Kept clear of both ends so a gridline label never lands on "turn 1" or "turn N".
    margin = turn_count * 0.06
    return [turn for turn in range(step, turn_count, step) if margin < turn < turn_count - margin]


def _select(
    spans: Sequence[Mapping[str, Any]], turn_count: int
) -> tuple[Sequence[Mapping[str, Any]], int]:
    """The spans to draw and how many were left out.

    Below the cap the payload's own order is kept untouched, so a small chart is unaffected by
    the existence of the cap. Above it, the spans are ranked by how long they stayed resident,
    which is the measure the chart is about.
    """
    if len(spans) <= MAX_SPANS:
        return spans, 0
    ranked = sorted(
        spans,
        key=lambda span: (-_turns_held(span, turn_count), str(span.get("item_id", ""))),
    )
    return ranked[:MAX_SPANS], len(spans) - MAX_SPANS


def _turns_held(span: Mapping[str, Any], turn_count: int) -> int:
    last_turn = span.get("last_turn")
    end_turn = turn_count if last_turn is None else int(last_turn)
    return end_turn - int(span["first_turn"]) + 1


def _session_names(spans: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Session id -> the short id that tells one span's session from another's.

    The full id would swamp the label; the first block identifies it among the few sessions a
    selection holds, and the row's tooltip carries the rest.
    """
    return {
        str(span.get("session_id")): str(span.get("session_id"))[:SHORT_ID_LENGTH] for span in spans
    }


def _span_row(
    *,
    span: Mapping[str, Any],
    chart_id: str,
    index: int,
    turn_count: int,
    top: int = 0,
    shared_prefix: str = "",
    draw_label: bool = True,
    spans_in_row: int = 1,
) -> str:
    """One item's bar, divided into its per-turn lanes, collapsed to runs.

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

    y = index * ROW_HEIGHT + top
    text_y = y + SPAN_HEIGHT + 1
    start_x = LABEL_GUTTER + TRACK_WIDTH * (first_turn - 1) // turn_count
    end_x = LABEL_GUTTER + TRACK_WIDTH * end_turn // turn_count
    width = max(2, end_x - start_x)

    lanes: Sequence[str] = span.get("lane_by_turn") or ()
    parts: list[str] = []
    if lanes:
        # Equal-width turns within the span: a turn is the unit the charge is levied on, so
        # every turn gets the same slice of the bar regardless of what it cost. The edges are
        # still computed per turn — a run is drawn from its first turn's left edge to its last
        # turn's right edge, so collapsing runs cannot move a boundary by a pixel.
        edges = [start_x + width * position // len(lanes) for position in range(len(lanes) + 1)]
        for lane, begin, end in _runs(lanes):
            lane_width = edges[end] - edges[begin]
            if lane_width <= 0:
                continue
            turns = (
                f"Turn {first_turn + begin}"
                if end - begin == 1
                else f"Turns {first_turn + begin}–{first_turn + end - 1} ({end - begin} turns)"
            )
            parts.append(
                f'<g class="mark"><rect class="slice lane lane--{escape(lane)}" '
                f'x="{edges[begin]}" y="{y}" width="{lane_width}" height="{SPAN_HEIGHT}" '
                f'fill="{_lane_fill(chart_id, lane)}"></rect>'
                f"<title>{turns}: {escape(LANE_MEANING.get(lane, lane))}</title></g>"
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
    label = (
        row_label(
            x=LABEL_GUTTER - 10,
            y=text_y,
            # One label per row. Where the item was resident in several sessions the row says
            # so, because "x26" is the answer to "why is this one item broken into bars".
            text=truncate(
                elide_prefix(display, shared_prefix)
                + (f" x{spans_in_row}" if spans_in_row > 1 else ""),
                LABEL_LIMIT,
            ),
            title=(
                f"{display} — resident in {spans_in_row} session(s)"
                if spans_in_row > 1
                else display
            ),
        )
        if draw_label
        else ""
    )
    return "".join(
        [
            label,
            "".join(parts),
            (
                f'<text class="row-value" x="{CHART_WIDTH}" y="{text_y}" text-anchor="end">'
                f"{turns_held} turns</text>"
                if draw_label
                else ""
            ),
            (
                f'<g class="mark"><rect class="hit" x="{start_x}" y="{y}" '
                f'width="{width}" height="{SPAN_HEIGHT}" fill="transparent"></rect>'
                f"<title>{escape(display)}: turns {first_turn}\u2013{end_turn} "
                f"({turns_held} turns) — {escape(reason)}</title></g>"
            ),
        ]
    )


def _runs(lanes: Sequence[str]) -> list[tuple[str, int, int]]:
    """Collapse consecutive equal lanes into ``(lane, begin, end)`` half-open ranges.

    Purely a serialisation concern: the runs cover exactly the same turns, in the same order,
    with the same lane on each.
    """
    runs: list[tuple[str, int, int]] = []
    for position, lane in enumerate(str(value) for value in lanes):
        if runs and runs[-1][0] == lane and runs[-1][2] == position:
            previous_lane, begin, _ = runs[-1]
            runs[-1] = (previous_lane, begin, position + 1)
        else:
            runs.append((lane, position, position + 1))
    return runs


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
