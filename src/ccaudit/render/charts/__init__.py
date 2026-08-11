"""Shared chart primitives — geometry, palette roles, and theming for hand-written SVG.

Charts are emitted as SVG built here in Python, with no charting library
(``specs/001-per-file-cost-attribution/research.md`` §1). Two reasons, both binding: every byte
of a library would ship inside every self-contained report (FR-032), and the visual rules this
project adopted — fixed-order categorical hues, the unattributed slice always drawn, light and
dark both selected, no distinction carried by colour alone — are rules we would otherwise spend
effort *imposing on* a library's defaults. Drawing the marks directly makes them the only way
to draw.

Three invariants hold for every mark produced here:

**Rectangles partition their parent exactly.** Widths are allocated with
:func:`ccaudit.money.allocate` — the same largest-remainder split that conserves micro-dollars
— so the pixels of a stacked bar sum to the bar, always. A chart that does not add up is the
same defect as a table that does not (Principle X). The 2px separator between segments is
drawn *over* the boundary rather than by shrinking the segments, so the visual gap costs no
geometry.

**Colour never carries a distinction on its own** (FR-042). Every slice is numbered, ordered,
directly labelled outside the fill, and repeated in a legend that is itself a small table of
figures. Text is never placed on a saturated fill, because a label that is legible on light
blue is not legible on dark yellow; the ordinal tick sits under the bar on the page surface
instead, where contrast is known in both themes.

**Money is never formatted here.** :func:`ccaudit.money.format_micros` renders every figure at
the ``display_sig_figs`` its confidence supports, and every absolute is paired with its share
in the same breath (FR-011, FR-095).

Palette values themselves live in ``assets/report.css`` as custom properties, so the light and
dark steps of a hue sit next to each other in one place and the SVG refers to roles
(``var(--series-1)``) rather than to hex.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from ccaudit.money import allocate, format_micros, format_share

# One fixed drawing width for every chart; the SVG scales to its container through the
# viewBox, so this is a coordinate system rather than a pixel size.
CHART_WIDTH = 720
BAR_HEIGHT = 40
ROW_HEIGHT = 26
LABEL_GUTTER = 210
SEPARATOR_WIDTH = 2
CORNER_RADIUS = 4

# Below this width a segment cannot carry its ordinal tick without the ticks colliding; the
# legend carries those entries instead. Nothing is dropped, only the direct label.
MIN_LABELLED_WIDTH = 26

# The categorical order is fixed and never cycled: a ninth series would be a generated hue,
# which is exactly the thing that makes two series indistinguishable under CVD.
SERIES_SLOT_COUNT = 8

# The remainder is not a series. It gets the neutral step plus a hatch, so it reads as "not
# one of the named things" in both themes and in print (FR-040, FR-042).
UNATTRIBUTED_SWATCH = "unattributed"

_HATCH_ID = "hatch"


@dataclass(frozen=True)
class Slice:
    """One part of a part-to-whole view, carrying everything needed to label it honestly."""

    label: str
    micros: int
    share: float
    sig_figs: int
    swatch: str
    detail: str = ""


def series_swatch(index: int) -> str:
    """The categorical swatch role for a zero-based series index.

    Raises past the eighth slot rather than wrapping around: a repeated hue silently merges two
    series into one, which is a wrong chart rather than an ugly one.
    """
    if index < 0 or index >= SERIES_SLOT_COUNT:
        raise ValueError(
            f"series index {index} is outside the fixed {SERIES_SLOT_COUNT}-slot categorical "
            f"order; fold the remaining entries into one labelled group rather than cycling hues"
        )
    return f"series-{index + 1}"


def partition(extent: int, weights: Sequence[int]) -> list[int]:
    """Split a pixel extent across weights so the parts sum to the extent exactly.

    The same largest-remainder allocation used for money, for the same reason: a proportional
    split that drops its remainder leaves a bar that does not reach its own end.
    """
    if extent < 0:
        raise ValueError(f"chart extent must be non-negative, got {extent}")
    if not weights:
        return []
    return allocate(extent, [max(0, weight) for weight in weights])


def money_share_html(micros: int, share: float, sig_figs: int, *, of_total: bool = True) -> str:
    """A figure and its share, as one indivisible pair of spans.

    Every absolute figure in the report goes through here, which is what makes "no dollar
    figure appears without its share" checkable by looking at the markup (FR-011). ``of_total``
    drops the three words for a table cell, where the column header already says it.
    """
    suffix = " of total" if of_total else ""
    return (
        f'<span class="money">{escape(format_micros(micros, sig_figs))}</span> '
        f'<span class="share">({escape(format_share(share))}{suffix})</span>'
    )


def money_share_text(micros: int, share: float, sig_figs: int) -> str:
    """The same pairing as plain text, for an SVG ``<title>`` or tick label."""
    return f"{format_micros(micros, sig_figs)} ({format_share(share)} of total)"


def hatch_defs(chart_id: str) -> str:
    """The one texture in the system: 45° lines, used only for the unattributed remainder."""
    return (
        f'<defs><pattern id="{escape(chart_id)}-{_HATCH_ID}" width="8" height="8" '
        f'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<rect width="8" height="8" fill="var(--unattributed)"></rect>'
        f'<rect width="3" height="8" fill="var(--unattributed-ink)" fill-opacity="0.55">'
        f"</rect></pattern></defs>"
    )


def swatch_fill(chart_id: str, swatch: str) -> str:
    """The paint for a swatch role — the hatch for the remainder, the hue otherwise."""
    if swatch == UNATTRIBUTED_SWATCH:
        return f"url(#{chart_id}-{_HATCH_ID})"
    return f"var(--{swatch})"


def figure(
    *,
    chart_id: str,
    title: str,
    svg: str,
    legend: str = "",
    note: str = "",
) -> str:
    """Wrap a chart as a figure: caption, marks, legend, and the note that qualifies it."""
    parts = [f'<figure class="chart" id="{escape(chart_id)}">']
    parts.append(f'<figcaption class="chart-title">{escape(title)}</figcaption>')
    parts.append(svg)
    if legend:
        parts.append(legend)
    if note:
        parts.append(f'<p class="chart-note">{escape(note)}</p>')
    parts.append("</figure>")
    return "".join(parts)


def legend_list(slices: Sequence[Slice]) -> str:
    """The legend, which doubles as the chart's table view.

    Ordered and numbered to match the marks, so identity survives greyscale, a colourblind
    reader, and a printer (FR-042). Zero-cost entries stay in the list rather than being tidied
    away — a slice that cost nothing is a finding, not a gap.
    """
    items = []
    for index, part in enumerate(slices, start=1):
        detail = f'<span class="legend-detail">{escape(part.detail)}</span>' if part.detail else ""
        items.append(
            f'<li class="legend-item">'
            f'<span class="swatch swatch--{escape(part.swatch)}" aria-hidden="true">{index}</span>'
            f'<span class="legend-label">{index}. {escape(part.label)}</span>'
            f"{money_share_html(part.micros, part.share, part.sig_figs)}"
            f"{detail}</li>"
        )
    return f'<ol class="legend">{"".join(items)}</ol>'


def svg_open(width: int, height: int, *, label: str) -> str:
    """An SVG root that scales to its container and announces itself to a screen reader."""
    return (
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" preserveAspectRatio="xMinYMin meet" role="img" '
        f'aria-label="{escape(label)}">'
    )


def rect(
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    fill: str,
    css_class: str,
    title: str = "",
    extra: str = "",
) -> str:
    """One data rectangle. ``title`` becomes a native tooltip — no JavaScript involved."""
    body = f"<title>{escape(title)}</title>" if title else ""
    return (
        f'<g class="mark">'
        f'<rect class="{css_class}" x="{x}" y="{y}" width="{width}" height="{height}" '
        f'fill="{fill}"{(" " + extra) if extra else ""}></rect>{body}</g>'
    )


def separators(boundaries: Sequence[int], *, y: int, height: int) -> str:
    """Surface-coloured dividers drawn *over* segment boundaries.

    Shrinking the segments to leave a gap would break the partition invariant, so the gap is
    painted on top instead: the geometry still adds up, the eye still sees the seam.
    """
    return "".join(
        f'<rect class="separator" x="{boundary - SEPARATOR_WIDTH // 2}" y="{y}" '
        f'width="{SEPARATOR_WIDTH}" height="{height}"></rect>'
        for boundary in boundaries
    )


def tick_label(*, x: int, y: int, text: str, anchor: str = "middle") -> str:
    """A label placed on the page surface, never on a fill.

    Text over a saturated segment is legible in one theme and not the other; a tick below the
    bar has known contrast in both (FR-041).
    """
    return f'<text class="tick" x="{x}" y="{y}" text-anchor="{anchor}">{escape(text)}</text>'


def row_label(*, x: int, y: int, text: str, anchor: str = "end") -> str:
    """A row's name, in the gutter beside its bar."""
    return f'<text class="row-label" x="{x}" y="{y}" text-anchor="{anchor}">{escape(text)}</text>'


def truncate(text: str, limit: int, *, keep_tail: bool = True) -> str:
    """Shorten a label, keeping the end that identifies it.

    A path's tail is what distinguishes it — its head is shared with everything else in the
    repository — so the ellipsis goes at the front by default. A bare name is the other way
    round: "unattributed" cut to its tail reads "…uted", which identifies nothing.
    """
    if limit <= 1 or len(text) <= limit:
        return text
    if keep_tail:
        return "…" + text[-(limit - 1) :]
    return text[: limit - 1] + "…"


def placeholder(*, chart_id: str, title: str, reason: str) -> str:
    """A view whose data this milestone does not yet produce.

    Stated as missing rather than filled with a plausible-looking shape. Missing attribution
    beats wrong attribution (Principle X), and that applies to pictures as much as to tables.
    """
    return (
        f'<figure class="chart chart--placeholder" id="{escape(chart_id)}">'
        f'<figcaption class="chart-title">{escape(title)}</figcaption>'
        f'<p class="placeholder-body"><strong>Not yet available.</strong> {escape(reason)} '
        f"Nothing is estimated in its place.</p></figure>"
    )
