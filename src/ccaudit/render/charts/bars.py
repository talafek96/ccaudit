"""Bar forms: the composition bar, the stacked per-item bars, and the cumulative sparkline.

Three questions, three forms. *What was the money spent on?* — one bar, the whole session,
every conclusion plus the remainder side by side (FR-040). *What did each item cost, and was
it the loading or the keeping?* — one stacked bar per item, on a common scale (FR-035).
*When did the money go?* — cumulative cost over the session, with compactions marked (FR-039).

Every one of them refuses to draw a set of parts that does not sum to the total it claims to
divide. That check is the whole point: a picture is the surface where a breakdown that does not
add up is least likely to be noticed and most likely to be believed.
"""

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

from ccaudit.money import format_micros, format_share
from ccaudit.render.charts import (
    BAR_HEIGHT,
    CHART_WIDTH,
    CORNER_RADIUS,
    LABEL_GUTTER,
    MIN_LABELLED_WIDTH,
    ROW_HEIGHT,
    Slice,
    figure,
    hatch_defs,
    legend_list,
    money_share_text,
    partition,
    placeholder,
    rect,
    row_label,
    separators,
    svg_open,
    swatch_fill,
    tick_label,
    truncate,
)

TICK_BASELINE = BAR_HEIGHT + 18
COMPOSITION_HEIGHT = TICK_BASELINE + 6

ROW_BAR_HEIGHT = 14
VALUE_GUTTER = 150
ROW_BAR_WIDTH = CHART_WIDTH - LABEL_GUTTER - VALUE_GUTTER
ROW_LABEL_LIMIT = 30

SPARK_HEIGHT = 170
# Wide enough for a money figure and its share: the y-axis label is a figure like any
# other, so it is paired with a share and must not be clipped to make room.
SPARK_PAD_LEFT = 150
SPARK_PAD_RIGHT = 16
SPARK_PAD_TOP = 16
SPARK_BASELINE = SPARK_HEIGHT - 34
SPARK_PLOT_WIDTH = CHART_WIDTH - SPARK_PAD_LEFT - SPARK_PAD_RIGHT
SPARK_PLOT_HEIGHT = SPARK_BASELINE - SPARK_PAD_TOP


def composition_bar(
    *,
    chart_id: str,
    title: str,
    slices: Sequence[Slice],
    total_micros: int,
    note: str = "",
) -> str:
    """One bar divided into the parts of a whole, with the remainder among them.

    Raises ``ValueError`` when the parts do not sum to ``total_micros``. A part-to-whole chart
    whose parts are not the whole is a lie told in a picture, and it is the one defect a reader
    cannot check by eye (Principle X, FR-012).
    """
    covered = sum(part.micros for part in slices)
    if covered != total_micros:
        raise ValueError(
            f"refusing to draw a part-to-whole chart whose parts do not add up: slices sum to "
            f"{covered} micro-dollars against a stated total of {total_micros}"
        )
    if total_micros <= 0:
        return placeholder(
            chart_id=chart_id,
            title=title,
            reason="No cost was recorded for this selection, so there is nothing to divide.",
        )

    drawn = [part for part in slices if part.micros > 0]
    widths = partition(CHART_WIDTH, [part.micros for part in drawn])

    marks: list[str] = []
    ticks: list[str] = []
    boundaries: list[int] = []
    x = 0
    for index, (part, width) in enumerate(zip(drawn, widths, strict=True), start=1):
        marks.append(
            rect(
                x=x,
                y=0,
                width=width,
                height=BAR_HEIGHT,
                fill=swatch_fill(chart_id, part.swatch),
                css_class="slice",
                title=f"{index}. {part.label} — {money_share_text(part.micros, part.share, part.sig_figs)}",
                extra=f'data-label="{escape(part.label)}"',
            )
        )
        if width >= MIN_LABELLED_WIDTH:
            ticks.append(tick_label(x=x + width // 2, y=TICK_BASELINE, text=str(index)))
        x += width
        if index < len(drawn):
            boundaries.append(x)

    svg = "".join(
        [
            svg_open(CHART_WIDTH, COMPOSITION_HEIGHT, label=f"{title}. Figures are in the legend."),
            hatch_defs(chart_id),
            (
                f'<clipPath id="{escape(chart_id)}-clip"><rect x="0" y="0" '
                f'width="{CHART_WIDTH}" height="{BAR_HEIGHT}" rx="{CORNER_RADIUS}">'
                f"</rect></clipPath>"
            ),
            f'<g clip-path="url(#{escape(chart_id)}-clip)">',
            "".join(marks),
            separators(boundaries, y=0, height=BAR_HEIGHT),
            "</g>",
            "".join(ticks),
            "</svg>",
        ]
    )
    return figure(chart_id=chart_id, title=title, svg=svg, legend=legend_list(slices), note=note)


def stacked_bars(
    *,
    chart_id: str,
    title: str,
    rows: Sequence[tuple[str, Sequence[Slice]]],
    legend: Sequence[Slice],
    total_micros: int,
    note: str = "",
) -> str:
    """One stacked bar per row, all on a common scale set by the largest row.

    Each row's segments partition that row's bar exactly, so the split between loading and
    keeping can be read off the picture and still matches the table beside it.
    """
    if not rows:
        return placeholder(
            chart_id=chart_id,
            title=title,
            reason="No items were attributed any cost in this selection.",
        )

    row_totals = [sum(part.micros for part in parts) for _, parts in rows]
    scale = max(row_totals)
    height = ROW_HEIGHT * len(rows) + 8

    body: list[str] = []
    for index, ((label, parts), row_total) in enumerate(zip(rows, row_totals, strict=True)):
        y = index * ROW_HEIGHT
        text_y = y + ROW_BAR_HEIGHT + 2
        body.append(
            row_label(
                x=LABEL_GUTTER - 10,
                y=text_y,
                text=truncate(label, ROW_LABEL_LIMIT),
                title=label,
            )
        )
        bar_width = 0 if scale <= 0 else ROW_BAR_WIDTH * row_total // scale
        widths = partition(bar_width, [part.micros for part in parts])
        x = LABEL_GUTTER
        boundaries: list[int] = []
        for position, (part, width) in enumerate(zip(parts, widths, strict=True), start=1):
            if width > 0:
                body.append(
                    rect(
                        x=x,
                        y=y,
                        width=width,
                        height=ROW_BAR_HEIGHT,
                        fill=swatch_fill(chart_id, part.swatch),
                        css_class="slice",
                        title=(
                            f"{label} — {part.label}: "
                            f"{money_share_text(part.micros, part.share, part.sig_figs)}"
                        ),
                        extra=f'data-label="{escape(part.label)}"',
                    )
                )
            x += width
            if position < len(parts) and 0 < width:
                boundaries.append(x)
        body.append(separators(boundaries, y=y, height=ROW_BAR_HEIGHT))
        sig_figs = min(part.sig_figs for part in parts)
        share = row_total / total_micros if total_micros else 0.0
        body.append(
            f'<text class="row-value" x="{CHART_WIDTH}" y="{text_y}" text-anchor="end">'
            f"{escape(format_micros(row_total, sig_figs))} "
            f"({escape(format_share(share))} of total)</text>"
        )

    svg = "".join(
        [
            svg_open(CHART_WIDTH, height, label=f"{title}. Figures are repeated beside each bar."),
            hatch_defs(chart_id),
            "".join(body),
            "</svg>",
        ]
    )
    return figure(chart_id=chart_id, title=title, svg=svg, legend=legend_list(legend), note=note)


def cumulative_sparkline(
    *,
    chart_id: str,
    title: str,
    turns: Sequence[Mapping[str, Any]],
    total_micros: int,
    sig_figs: int,
    note: str = "",
) -> str:
    """Cost accumulating over the session, with compaction events marked (FR-039).

    The line is cumulative rather than per-turn because the question it answers is "when had I
    already spent most of it", and compactions are marked because a compaction is the single
    event that most changes what the following turns cost.
    """
    if not turns:
        return placeholder(
            chart_id=chart_id,
            title=title,
            reason=(
                "Per-turn figures are not yet computed, so cost over the course of the session "
                "cannot be drawn."
            ),
        )

    charged = sum(int(turn["cost_micros"]) for turn in turns)
    if charged != total_micros:
        # The curve ends at the session total by definition — every charge belongs to a turn.
        # If it does not, the last point would be labelled as some percentage other than 100%
        # of a total it is supposed to *be*, which is a figure contradicting its own total.
        raise ValueError(
            f"refusing to draw cost over the session: per-turn figures sum to {charged} "
            f"micro-dollars against a session total of {total_micros}"
        )

    running = 0
    points: list[tuple[float, float, Mapping[str, Any], int]] = []
    span = max(1, len(turns) - 1)
    peak = max(1, charged)
    for index, turn in enumerate(turns):
        running += int(turn["cost_micros"])
        x = SPARK_PAD_LEFT + round(SPARK_PLOT_WIDTH * index / span, 1)
        y = round(SPARK_BASELINE - SPARK_PLOT_HEIGHT * running / peak, 1)
        points.append((x, y, turn, running))

    line = "M " + " L ".join(f"{x} {y}" for x, y, _, _ in points)
    area = (
        f"{line} L {points[-1][0]} {SPARK_BASELINE} L {points[0][0]} {SPARK_BASELINE} Z"
        if len(points) > 1
        else ""
    )

    marks: list[str] = []
    if area:
        marks.append(f'<path class="spark-area" d="{area}"></path>')
    marks.append(f'<path class="spark-line" d="{line}"></path>')

    for x, y, turn, running_total in points:
        compaction = turn.get("compaction") or {}
        if not compaction.get("occurred"):
            continue
        share = running_total / total_micros if total_micros else 0.0
        marks.append(
            f'<line class="spark-event" x1="{x}" y1="{SPARK_PAD_TOP}" x2="{x}" '
            f'y2="{SPARK_BASELINE}"></line>'
        )
        # A marker shape as well as a dashed rule: the event must survive greyscale (FR-042).
        marks.append(
            f'<g class="mark"><circle class="spark-point" cx="{x}" cy="{y}" r="5"></circle>'
            f"<title>Turn {escape(str(turn['ordinal']))}: conversation compacted. Cost so far "
            f"{escape(money_share_text(running_total, share, sig_figs))}</title></g>"
        )
        marks.append(
            tick_label(x=round(x), y=SPARK_PAD_TOP - 4, text=f"compacted (turn {turn['ordinal']})")
        )

    axis = "".join(
        [
            (
                f'<line class="axis" x1="{SPARK_PAD_LEFT}" y1="{SPARK_BASELINE}" '
                f'x2="{CHART_WIDTH - SPARK_PAD_RIGHT}" y2="{SPARK_BASELINE}"></line>'
            ),
            (
                f'<text class="tick" x="0" y="{SPARK_BASELINE + 4}" '
                f'text-anchor="start">$0 (0.0% of total)</text>'
            ),
            (
                f'<text class="tick" x="0" y="{SPARK_PAD_TOP + 4}" text-anchor="start">'
                f"{escape(money_share_text(peak, peak / total_micros if total_micros else 0.0, sig_figs))}"
                f"</text>"
            ),
            tick_label(x=SPARK_PAD_LEFT, y=SPARK_BASELINE + 18, text=f"turn {turns[0]['ordinal']}"),
            tick_label(
                x=CHART_WIDTH - SPARK_PAD_RIGHT,
                y=SPARK_BASELINE + 18,
                text=f"turn {turns[-1]['ordinal']}",
            ),
        ]
    )

    svg = "".join(
        [
            svg_open(CHART_WIDTH, SPARK_HEIGHT, label=f"{title}. Cumulative cost by turn."),
            axis,
            "".join(marks),
            "</svg>",
        ]
    )
    return figure(chart_id=chart_id, title=title, svg=svg, note=note)
