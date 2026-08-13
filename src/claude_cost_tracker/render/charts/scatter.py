"""The cause plot: what each item cost, against how often it was read.

This is the one picture that carries the project's central claim. Ranking files by cost and
ranking them by read count produce **different lists** — on this repo's own session they were
completely disjoint in the top five — because content is charged twice: once to load, and again
on every later turn it stays resident. A table can state that. Only a plot makes it obvious,
because the finding *is* a shape: the expensive items are not on the right of the plot with the
often-read ones, they are up the left-hand side, read once and held for hundreds of turns.

So the axes are chosen to make the wrong intuition visibly wrong:

- **x — how many times it was read.** The measure a read counter would rank by.
- **y — what it actually cost.** The measure that matters.

An item high and to the left cost a lot without being read much: it is big and it sat there,
and the fix is to keep it out of context, not to read it less. An item on the right earned its
cost by being fetched repeatedly, and the fix is the opposite. Each point says which by its
fill, so the two populations are separable without reading a single number.

Both axes are logarithmic, and that is a presentation decision that must be **stated on the
chart**, not inferred: reads span 1 to ~900 and costs span four orders of magnitude, so a
linear axis would pile every point into one corner and hide the finding this plot exists to
show. Ticks are labelled with their real values so no reader has to undo a scale in their head.
"""

import math
from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

from claude_cost_tracker.money import format_axis_micros, format_micros, format_share
from claude_cost_tracker.render.charts import (
    CHART_WIDTH,
    SERIES_SLOT_COUNT,
    figure,
    gridlines,
    money_gridline_values,
    money_share_text,
    placeholder,
    series_swatch,
    svg_open,
    tick_label,
    truncate,
)

PLOT_HEIGHT = 300
PLOT_LEFT = 74
PLOT_RIGHT = CHART_WIDTH - 16
PLOT_TOP = 12
PLOT_BOTTOM = PLOT_HEIGHT - 42

# A point's radius carries the item's size in tokens — a third measure, and the one that
# explains *why* a rarely-read item is expensive. Bounded at both ends: below the floor a point
# is invisible, above the ceiling it swallows its neighbours.
MIN_RADIUS = 3
MAX_RADIUS = 13

# Above this share of an item's cost, the carry side is what the item cost, and the point is
# drawn as "held" rather than "read". Not a hair-splitting threshold: the populations this plot
# separates sit at roughly 5% and 95%, so anything near the middle is genuinely mixed and is
# labelled as such.
HELD_THRESHOLD = 0.6
READ_THRESHOLD = 0.4

# How many items get a point. Beyond this the plot is a smear, and the table below it is the
# better surface. Whatever is dropped is stated in the drawing (never silently).
MAX_POINTS = 120

# How close two tick labels may sit before one of them is dropped. A money label is about
# five characters wide at the axis font and a line is about that tall, so these are the widths
# below which the two would be drawn over one another.
MIN_TICK_GAP_X = 34
MIN_TICK_GAP_Y = 14

# A session name is prose and can run long; the gutter holds about this much.
SESSION_LABEL_LIMIT = 30

# One cent, in micro-dollars. The finest money the display ever shows, and therefore the
# smallest axis tick that can carry a distinct label.
CENT = 10_000


def _log_scale(value: float, low: float, high: float, start: int, end: int) -> int:
    """Place a value on a true log axis, with everything floored at 1.

    This used to shift value and bounds by one "so zero fits". Nothing ever needs that: cost is
    counted in micro-dollars and an item is *in* context because it was loaded, so reads start
    at one. What the shift did do was bend the axis — with a shift, consecutive powers of ten
    are no longer equally spaced, so the reads gridlines came out 232px and 303px apart on an
    axis whose whole premise is that a step right is a multiplication.

    The floor is kept as a guard rather than an assumption: a log axis has no zero, so a zero
    is placed at the axis floor instead of crashing.
    """
    low = max(low, 1.0)
    high = max(high, low)
    if high <= low:
        return start
    position = (math.log10(max(value, 1.0)) - math.log10(low)) / (
        math.log10(high) - math.log10(low)
    )
    return round(start + position * (end - start))


def _cause(item: Mapping[str, Any]) -> tuple[str, str]:
    """Which side an item's cost came from, as a swatch role and a plain-language name."""
    total = int(item["total_micros"])
    if total <= 0:
        return "muted", "no cost"
    carry_share = int(item["carry_micros"]) / total
    if carry_share >= HELD_THRESHOLD:
        return "series-2", "mostly the keeping"
    if carry_share <= READ_THRESHOLD:
        return "series-1", "mostly the loading"
    return "series-4", "an even mix"


def cause_scatter(
    items: Sequence[Mapping[str, Any]],
    *,
    chart_id: str = "cause-plot",
    title: str = "What it cost, against how often it was read",
) -> str:
    """Cost against read count, one point per item, sized by tokens and filled by cause."""
    priced = [item for item in items if int(item["total_micros"]) > 0]
    if not priced:
        return placeholder(
            chart_id=chart_id,
            title=title,
            reason="No item in this selection was attributed any cost.",
        )

    ranked = sorted(priced, key=lambda item: (-int(item["total_micros"]), str(item["display"])))
    drawn = ranked[:MAX_POINTS]
    omitted = len(ranked) - len(drawn)

    # Snapped out to whole powers of ten, so every horizontal line is a decade and they are
    # therefore evenly spaced. Clamped to the data instead, the top and bottom lines were the
    # priciest and cheapest items — landing 0.88 and 1.41 decades from their neighbours, so an
    # axis that is uniform everywhere else looked broken at both ends, and the bottom label
    # named a position whose value a reader could not work out.
    #
    # Still not anchored at zero: a log axis has no zero, and starting a decade below the
    # cheapest item keeps the spread the chart exists to show.
    max_cost = _decade_above(max(int(item["total_micros"]) for item in drawn))
    min_cost = _decade_below(min(int(item["total_micros"]) for item in drawn))
    # Snapped out to a decade for the same reason as the cost axis: left at the data maximum,
    # the last vertical line sat a fraction of a decade from its neighbour while every other
    # gap was a full one.
    max_reads = _decade_above(max(int(item["reads"]) for item in drawn))
    max_size = max(int(item["size_tokens"]) for item in drawn) or 1
    # A second way to read the same points. Cause answers "what kind of cost is this"; category
    # answers "what kind of *thing* is this", which is the question when you are deciding what
    # to stop reading. Both fills ship, and the reader picks.
    categories = sorted({str(item["category"]) for item in drawn})
    category_fills = {
        name: series_swatch(index % SERIES_SLOT_COUNT) for index, name in enumerate(categories)
    }

    body = [_grid(max_reads, min_cost, max_cost)]
    for item in drawn:
        swatch, cause = _cause(item)
        x = _log_scale(int(item["reads"]), 1, max_reads, PLOT_LEFT, PLOT_RIGHT)
        y = _log_scale(int(item["total_micros"]), min_cost, max_cost, PLOT_BOTTOM, PLOT_TOP)
        radius = MIN_RADIUS + round(
            (MAX_RADIUS - MIN_RADIUS) * math.sqrt(int(item["size_tokens"]) / max_size)
        )
        figures = int(item["display_sig_figs"])
        # The **full** path, not the shortened one. A tooltip carrying the same truncation as
        # the chart answers nothing: the reason to hover a point is that its label is not enough
        # to tell which file it is.
        detail = (
            f"{item['display']}\n"
            f"{money_share_text(int(item['total_micros']), float(item['share']), figures)}; "
            f"read {int(item['reads']):,} time(s), resident for "
            f"{int(item['turns_resident']):,} turn(s), {int(item['size_tokens']):,} tokens; "
            f"{cause}; {item['category']}"
        )
        category_swatch = category_fills[str(item["category"])]
        body.append(
            f'<g class="mark" data-fill-cause="{escape(swatch)}" '
            f'data-fill-category="{escape(category_swatch)}">'
            f'<circle class="point point--{escape(swatch)}" cx="{x}" cy="{y}" '
            f'r="{radius}" fill="var(--{escape(swatch)})"></circle>'
            f"<title>{escape(detail)}</title></g>"
        )

    dropped = (
        f" The {omitted} cheapest item(s) are not plotted — they are in the table below, and "
        f"they are in every total on this page."
        if omitted
        else ""
    )
    note = (
        "Both axes are logarithmic, so a step right or up is a multiplication rather than an "
        "addition; the ticks carry the real values. A point's area is the item's size in "
        "tokens. Up and to the left is the finding this tool exists for: expensive without "
        "being read much, because it was large and it stayed resident." + dropped
    )
    controls = (
        '<p class="fill-switch js-only" data-fill-switch>'
        '<button type="button" class="fill-btn" data-fill="cause" aria-pressed="true">'
        "Colour by cause</button>"
        '<button type="button" class="fill-btn" data-fill="category" aria-pressed="false">'
        "Colour by category</button></p>"
    )
    return figure(
        chart_id=chart_id,
        title=title,
        svg=(
            controls
            + svg_open(CHART_WIDTH, PLOT_HEIGHT, label=title)
            + "".join(body)
            + _axis_labels()
            + "</svg>"
        ),
        legend=_legend() + _category_legend(category_fills),
        note=note,
    )


def _grid(max_reads: int, min_cost: int, max_cost: int) -> str:
    """Axis lines, gridlines, and ticks, labelled with real values rather than log positions.

    Both axes are logarithmic, so the gap between two points says nothing on its own — the
    lines are what turn a cloud of dots into readable values.
    """
    parts = [
        (
            f'<line class="axis" x1="{PLOT_LEFT}" y1="{PLOT_TOP}" x2="{PLOT_LEFT}" '
            f'y2="{PLOT_BOTTOM}"></line>'
        ),
        (
            f'<line class="axis" x1="{PLOT_LEFT}" y1="{PLOT_BOTTOM}" x2="{PLOT_RIGHT}" '
            f'y2="{PLOT_BOTTOM}"></line>'
        ),
    ]
    # Distinct *text* is not enough: the range ends are added as ticks, and on a log axis an
    # end can land a pixel from the power of ten below it, printing two labels on top of each
    # other. A tick that cannot be read is worse than a missing one, so crowded ones are dropped.
    drawn_x: list[int] = []
    for reads in _ticks(max_reads):
        x = _log_scale(reads, 1, max_reads, PLOT_LEFT, PLOT_RIGHT)
        if any(abs(x - other) < MIN_TICK_GAP_X for other in drawn_x):
            continue
        drawn_x.append(x)
        parts.append(tick_label(x=x, y=PLOT_BOTTOM + 14, text=f"{reads:,}"))
        parts.append(gridlines([x], span=(PLOT_TOP, PLOT_BOTTOM)))
    parts.append(
        gridlines(
            [
                _log_scale(value, 1, max_reads, PLOT_LEFT, PLOT_RIGHT)
                for value in _minor_values(1, max_reads)
            ],
            span=(PLOT_TOP, PLOT_BOTTOM),
            css_class="gridline gridline--minor",
        )
    )
    parts.append(
        gridlines(
            [
                _log_scale(value, min_cost, max_cost, PLOT_BOTTOM, PLOT_TOP)
                for value in _minor_values(min_cost, max_cost)
            ],
            span=(PLOT_LEFT, PLOT_RIGHT),
            vertical=False,
            css_class="gridline gridline--minor",
        )
    )
    drawn_y: list[int] = []
    for cost in _money_ticks(min_cost, max_cost):
        y = _log_scale(cost, min_cost, max_cost, PLOT_BOTTOM, PLOT_TOP)
        if any(abs(y - other) < MIN_TICK_GAP_Y for other in drawn_y):
            continue
        drawn_y.append(y)
        parts.append(
            tick_label(x=PLOT_LEFT - 6, y=y + 4, text=format_axis_micros(cost), anchor="end")
        )
        parts.append(gridlines([y], span=(PLOT_LEFT, PLOT_RIGHT), vertical=False))
    return "".join(parts)


def _decade_below(value: int) -> int:
    """The power of ten at or below ``value`` — the axis floor."""
    if value <= 0:
        return 1
    return int(10 ** math.floor(math.log10(value)))


def _decade_above(value: int) -> int:
    """The power of ten at or above ``value`` — the axis ceiling."""
    if value <= 0:
        return 1
    return int(10 ** math.ceil(math.log10(value)))


def _minor_values(lowest: float, highest: float) -> list[float]:
    """The 2..9 subdivisions inside each decade of a log range.

    A log axis ruled only at the powers of ten leaves a decade-wide gap in which every position
    is a guess — and on this plot most of the points live inside one such gap. The subdivisions
    go unlabelled: their spacing is the information, and nine numbers per decade would bury the
    ones that matter.
    """
    if lowest <= 0 or highest <= lowest:
        return []
    values: list[float] = []
    decade = 10.0 ** math.floor(math.log10(lowest))
    while decade <= highest:
        for multiple in range(2, 10):
            value = decade * multiple
            if lowest < value < highest:
                values.append(value)
        decade *= 10
    return values


def _money_ticks(lowest: int, highest: int) -> list[int]:
    """Every power of ten across the range, which is where the gridlines go.

    The range is decade-aligned by the caller, so the first and last ticks *are* the ends and
    nothing extra has to be appended for them. Labelled with `format_axis_micros`, because the
    figure formatter collapses everything under half a cent to "<$0.01" and three of these
    decades are below a cent — a ladder of identical labels says nothing.
    """
    if lowest <= 0:
        return []
    values: list[int] = []
    tick = lowest
    while tick <= highest:
        values.append(tick)
        tick *= 10
    return values


def _ticks(highest: int) -> list[int]:
    """Powers of ten up to the maximum, which is decade-aligned by the caller.

    Nothing extra is appended for the end: the end *is* a power of ten, so appending it would
    duplicate the last tick — and when it was not, that final tick landed a fraction of a
    decade from its neighbour and made an otherwise uniform axis look wrong.
    """
    values = [1]
    while values[-1] * 10 <= highest:
        values.append(values[-1] * 10)
    return values


def _axis_labels() -> str:
    return (
        f'<text class="tick" x="{(PLOT_LEFT + PLOT_RIGHT) // 2}" y="{PLOT_HEIGHT - 10}" '
        f'text-anchor="middle">times read (log)</text>'
        f'<text class="tick" x="{PLOT_LEFT - 60}" y="{(PLOT_TOP + PLOT_BOTTOM) // 2}" '
        f'text-anchor="middle" transform="rotate(-90 {PLOT_LEFT - 60} '
        f'{(PLOT_TOP + PLOT_BOTTOM) // 2})">estimated cost (log)</text>'
    )


def _legend() -> str:
    """What a fill means, in words — colour never carries a distinction alone (FR-042)."""
    entries = [
        (
            "series-2",
            "Mostly the keeping",
            "large, resident a long time; read count is not the lever",
        ),
        ("series-1", "Mostly the loading", "fetched repeatedly; reading it less is the lever"),
        ("series-4", "An even mix", "both causes contribute materially"),
    ]
    items = "".join(
        f'<li class="legend-item">'
        f'<span class="swatch swatch--{swatch}" aria-hidden="true">•</span>'
        f'<span class="legend-label">{escape(name)}</span>'
        f'<span class="legend-detail">{escape(detail)}</span></li>'
        for swatch, name, detail in entries
    )
    return f'<ul class="legend" data-fill-legend="cause">{items}</ul>'


def _category_legend(fills: dict[str, str]) -> str:
    """Named categories, hidden until the reader switches to colouring by them."""
    items = "".join(
        f'<li class="legend-item">'
        f'<span class="swatch swatch--{escape(swatch)}" aria-hidden="true">\u2022</span>'
        f'<span class="legend-label">{escape(name)}</span></li>'
        for name, swatch in fills.items()
    )
    return f'<ul class="legend legend--category" data-fill-legend="category" hidden>{items}</ul>'


def session_bars(
    sessions: Sequence[Mapping[str, Any]],
    *,
    chart_id: str = "sessions",
    title: str = "What each session cost, and why",
) -> str:
    """One stacked bar per session: loading, keeping, and everything else.

    Drawn only for a selection of more than one session — for a single session it would restate
    the headline figure as a picture of one bar.

    The three segments partition the session total exactly, which is what makes the bar
    readable as a whole rather than as three unrelated measures side by side.
    """
    if len(sessions) < 2:
        return ""

    widest = max(int(row["cost_micros"]) for row in sessions) or 1
    row_height = 30
    bar_height = 16
    gutter = 260
    value_gutter = 210
    span = CHART_WIDTH - gutter - value_gutter
    ruled = money_gridline_values(widest)
    plot_bottom = row_height * len(sessions)
    height = plot_bottom + (28 if ruled else 10)

    # Ruled in money like the item bars, so "which session cost the most" and "how much did it
    # cost" are both answerable from the same picture.
    body = [
        gridlines([gutter + span * value // widest for value in ruled], span=(0, plot_bottom)),
        "".join(
            tick_label(
                x=gutter + span * value // widest,
                y=plot_bottom + 15,
                text=format_micros(value, 2),
            )
            for value in ruled
        ),
    ]
    for index, row in enumerate(sessions):
        y = index * row_height
        total = int(row["cost_micros"])
        figures = int(row["display_sig_figs"])
        # The name is what tells a reader which session this bar is; the id fragment beside it
        # is what they type to select it. `display_name` carries both, from one place.
        label = str(row.get("display_name") or row["session_id"][:8])
        body.append(
            f'<text class="row-label" x="{gutter - 10}" y="{y + bar_height}" '
            f'text-anchor="end">{escape(truncate(label, SESSION_LABEL_LIMIT))}'
            f"<title>{escape(label)} — {escape(str(row['session_id']))}</title></text>"
        )
        x = gutter
        parts = (
            ("direct_micros", "series-1", "loading into context"),
            ("carry_micros", "series-2", "keeping context loaded"),
            ("other_micros", "series-4", "output, the conversation itself, and the remainder"),
        )
        for field, swatch, meaning in parts:
            value = max(0, int(row[field]))
            width = 0 if total <= 0 else round(span * value / widest)
            if width <= 0:
                continue
            # Shares are of *this session*, not of the selection: the bar divides one session,
            # so a share against the corpus total would not sum to the thing being divided.
            body.append(
                f'<g class="mark"><rect class="slice" x="{x}" y="{y + 4}" width="{width}" '
                f'height="{bar_height}" fill="var(--{swatch})"></rect>'
                f"<title>{escape(label)} — {escape(meaning)}: "
                f"{escape(format_micros(value, figures))} "
                f"({escape(format_share(value / total if total else 0.0))} of this session)"
                f"</title></g>"
            )
            x += width
        # Cost and share only. The turn count is real but it is a third measure competing for
        # the same strip, and on the longest bar it ran under the bar itself; it moves to the
        # hover, where it is available without crowding the figure that ranks the row.
        running = " · running" if row["provisional"] else ""
        body.append(
            f'<text class="row-value" x="{CHART_WIDTH}" y="{y + bar_height}" '
            f'text-anchor="end">{escape(format_micros(total, figures))} '
            f"({escape(format_share(float(row['share'])))}){escape(running)}"
            f"<title>{escape(str(row['session_id']))} — {int(row['turns']):,} turns</title>"
            f"</text>"
        )

    note = (
        "Bars share one scale, so their lengths are comparable. Each is divided into what it "
        "cost to load content in, what it cost to keep it there, and everything else — the "
        "three add up to that session's total."
    )
    return figure(
        chart_id=chart_id,
        title=title,
        svg=svg_open(CHART_WIDTH, height, label=title) + "".join(body) + "</svg>",
        note=note,
    )
