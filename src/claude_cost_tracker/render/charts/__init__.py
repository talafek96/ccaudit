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
:func:`claude_cost_tracker.money.allocate` — the same largest-remainder split that conserves micro-dollars
— so the pixels of a stacked bar sum to the bar, always. A chart that does not add up is the
same defect as a table that does not (Principle X). The 2px separator between segments is
drawn *over* the boundary rather than by shrinking the segments, so the visual gap costs no
geometry.

**Colour never carries a distinction on its own** (FR-042). Every slice is numbered, ordered,
directly labelled outside the fill, and repeated in a legend that is itself a small table of
figures. Text is never placed on a saturated fill, because a label that is legible on light
blue is not legible on dark yellow; the ordinal tick sits under the bar on the page surface
instead, where contrast is known in both themes.

**Money is never formatted here.** :func:`claude_cost_tracker.money.format_micros` renders every figure at
the ``display_sig_figs`` its confidence supports, and every absolute is paired with its share
in the same breath (FR-011, FR-095).

Palette values themselves live in ``assets/report.css`` as custom properties, so the light and
dark steps of a hue sit next to each other in one place and the SVG refers to roles
(``var(--series-1)``) rather than to hex.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from claude_cost_tracker.money import allocate, format_micros, format_share

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


def gridlines(
    positions: Sequence[int],
    *,
    span: tuple[int, int],
    vertical: bool = True,
    css_class: str = "gridline",
) -> str:
    """Faint reference lines at ``positions``, spanning ``span``.

    A bar you can only compare to its neighbours answers "which is biggest"; a bar crossed by
    a labelled scale answers "how much". The lines are drawn first so every mark sits on top of
    them, and they carry no title — they are furniture, not data, and a tooltip on one would
    put a hit target over the marks that matter.
    """
    if not positions:
        return ""
    low, high = span
    if vertical:
        return "".join(
            f'<line class="{css_class}" x1="{at}" y1="{low}" x2="{at}" y2="{high}"></line>'
            for at in positions
        )
    return "".join(
        f'<line class="{css_class}" x1="{low}" y1="{at}" x2="{high}" y2="{at}"></line>'
        for at in positions
    )


def money_gridline_values(scale_micros: int, divisions: int = 4) -> list[int]:
    """Round money values to rule a bar chart at, from zero up to ``scale_micros``.

    Rounded to something a reader would say out loud — $5, $10, $50 — rather than to the
    arithmetic quarters of whatever the largest bar happens to be, because the point of the
    line is to be a landmark and $23.61 is not one.
    """
    if scale_micros <= 0 or divisions <= 0:
        return []
    rough = scale_micros / divisions
    magnitude = 10 ** math.floor(math.log10(rough)) if rough > 0 else 1
    for multiple in (1, 2, 2.5, 5, 10):
        step = magnitude * multiple
        if step >= rough:
            break
    values: list[int] = []
    seen: set[str] = set()
    at = step
    while at <= scale_micros:
        # A ruler is only useful if its marks can be told apart. On a small scale the steps all
        # render as "<$0.01" — three identical labels claiming to be different positions, and a
        # money figure with no share beside it, which is a thing this project does not print.
        # Where the scale is too fine to label honestly, it goes unruled.
        label = format_micros(int(at), 2)
        if not label.startswith("<") and label not in seen:
            seen.add(label)
            values.append(int(at))
        at += step
    return values


def tick_label(*, x: int, y: int, text: str, anchor: str = "middle") -> str:
    """A label placed on the page surface, never on a fill.

    Text over a saturated segment is legible in one theme and not the other; a tick below the
    bar has known contrast in both (FR-041).
    """
    return f'<text class="tick" x="{x}" y="{y}" text-anchor="{anchor}">{escape(text)}</text>'


def row_label(*, x: int, y: int, text: str, anchor: str = "end", title: str = "") -> str:
    """A row's name, in the gutter beside its bar.

    ``title`` is the untruncated name, shown as a native tooltip on hover. A shortened label
    that offers no way back to the full one makes two different files look like the same row,
    so any caller that truncates must pass the original here.
    """
    tip = f"<title>{escape(title)}</title>" if title and title != text else ""
    return (
        f'<text class="row-label" x="{x}" y="{y}" text-anchor="{anchor}">{escape(text)}{tip}</text>'
    )


# Below this, a middle ellipsis leaves too few characters on either side to identify anything,
# so the label degrades to a single head fragment instead of two useless ones.
MIDDLE_TRUNCATION_FLOOR = 8


def shortest_unique_labels(paths: Sequence[str]) -> list[str]:
    """The shortest tail of each path that still tells it apart from the others.

    Squeezing a full path into a chart gutter by cutting its middle produces labels like
    ``…/specs/001-pe…ibution/spec.md`` — two ellipses, and two sibling files reduced to nearly
    the same string. The reader cannot tell the rows apart, which is the one job a row label
    has.

    So this does what an editor does with tab titles: start at the file name, and give back a
    parent directory only to the labels that would otherwise collide. ``spec.md`` and
    ``plan.md`` need nothing more; two ``__init__.py`` files grow to ``config/__init__.py`` and
    ``render/__init__.py``, and stop there. Every label is a real, contiguous tail of its path —
    never an elision — so what is on screen can be matched against the full name in the tooltip
    without decoding anything.
    """
    labels = [path.replace("\\", "/").rstrip("/").split("/")[-1] or path for path in paths]
    depth = 1
    # A path cannot have more segments than it has characters, so this terminates; the bound is
    # a guard against a pathological input rather than an expected exit.
    while depth < 64:
        counts: dict[str, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        clashing = [index for index, label in enumerate(labels) if counts[label] > 1]
        if not clashing:
            break
        depth += 1
        grown = False
        for index in clashing:
            segments = paths[index].replace("\\", "/").rstrip("/").split("/")
            if len(segments) >= depth:
                labels[index] = "/".join(segments[-depth:])
                grown = True
        if not grown:
            # Genuinely identical paths. Nothing further to distinguish them by, and inventing a
            # suffix would be labelling them with something that is not their name.
            break
    return labels


def common_directory_prefix(labels: Sequence[str]) -> str:
    """The leading path segments every label shares, or "" when they share none.

    Truncation keeps both ends of a label because both carry identity — but a head that reads
    ``/Users/someone/`` on every row carries none, and spends half the width saying so. Cutting
    the shared part first is what makes the kept head worth keeping. Whole segments only: half
    a directory name is a different directory.
    """
    paths = [label for label in labels if "/" in label]
    if len(paths) < 2:
        return ""
    segments = [label.split("/")[:-1] for label in paths]
    shared: list[str] = []
    for parts in zip(*segments, strict=False):
        if len({*parts}) != 1:
            break
        shared.append(parts[0])
    # One shared segment is usually just the leading "" of an absolute path — not worth a cut.
    if len(shared) < 2:
        return ""
    return "/".join(shared) + "/"


def elide_prefix(label: str, prefix: str) -> str:
    """Drop a shared leading path, marking the cut so the label is not mistaken for relative."""
    if prefix and label.startswith(prefix):
        return "…/" + label[len(prefix) :]
    return label


def truncate(text: str, limit: int) -> str:
    """Shorten a label by removing its middle, keeping both ends.

    Both ends carry identity and neither alone is enough: the tail distinguishes a path from
    its siblings ("…/model/lanes.py"), while the head says which tree it is in — two files
    named ``__init__.py`` in different packages are one indistinguishable row without it. So
    the cut goes where the least information is, in the middle, and the full name travels with
    the label as a tooltip rather than being lost.
    """
    if limit <= 1 or len(text) <= limit:
        return text
    if limit < MIDDLE_TRUNCATION_FLOOR:
        return text[: limit - 1] + "…"
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return text[:head] + "…" + text[-tail:]


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
