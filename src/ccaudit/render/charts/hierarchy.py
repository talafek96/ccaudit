"""The icicle — cost over the folder tree (FR-034).

An icicle rather than a treemap, for one reason: a treemap's rectangles are compared by area,
and area is the measure people read worst. An icicle keeps every node's cost on a single
horizontal scale, so two folders three levels apart are still compared by the length of a bar,
and the tree structure is carried by the vertical axis instead of by nesting.

Each node shows both measures the contract carries: the width is its ``total_micros``
(everything below it), and its own ``flat_micros`` is drawn as an explicitly labelled block
inside it. That is the "own cost versus everything it caused" switch of FR-034, rendered rather
than toggled — the toggle needs a data path this milestone does not have, and a static drawing
of both is honest where a control that does nothing is not.

Text does sit on the fills here, so the fills are a single hue at low opacity with a solid
stroke: depth reads as a sequential ramp, and the label keeps page-surface contrast in both
themes (FR-041).
"""

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

from ccaudit.money import format_micros
from ccaudit.render.charts import (
    CHART_WIDTH,
    ROW_HEIGHT,
    UNATTRIBUTED_SWATCH,
    figure,
    hatch_defs,
    money_share_text,
    partition,
    placeholder,
    svg_open,
    swatch_fill,
    truncate,
)

# How deep the drawing goes. This used to be 6, which stopped at folders and cut the files
# underneath them — and since zooming only rescales nodes that are already in the drawing, the
# files were unreachable rather than merely hidden. A flame graph whose leaves are folders is
# not showing you what cost the money.
#
# Depth is bounded by a node budget instead, so a pathologically deep corpus cannot produce a
# megabyte of rectangles; whatever the budget drops is stated in the chart (never silently).
MAX_DEPTH = 24
MAX_NODES = 1_500
NODE_HEIGHT = ROW_HEIGHT
# Roughly 7px per character at the label size; below this a node cannot show even a stub.
PIXELS_PER_CHARACTER = 7
MIN_LABELLED_NODE = 34
OWN_COST_LABEL = "own"

# One hue, fading with depth: magnitude-by-depth is a sequential encoding, and a rainbow of
# depths would imply the levels are unrelated categories.
DEPTH_OPACITY = (0.55, 0.44, 0.34, 0.26, 0.20, 0.15)


def icicle(
    *,
    chart_id: str,
    title: str,
    tree: Mapping[str, Any],
    total_micros: int,
    note: str = "",
) -> str:
    """Draw the folder hierarchy as an icicle, remainder node included.

    ``tree`` is the ``tree`` section of the report-data payload. An empty mapping means the
    analysis does not yet produce a hierarchy, which is said plainly rather than drawn as an
    empty box.
    """
    if not tree:
        return placeholder(
            chart_id=chart_id,
            title=title,
            reason=(
                "Cost has not yet been rolled up over the folder tree, so the hierarchy cannot "
                "be drawn."
            ),
        )

    rows: list[str] = []
    depth_reached = _draw(
        node=tree,
        chart_id=chart_id,
        x=0,
        width=CHART_WIDTH,
        span=(0.0, 1.0),
        depth=0,
        total_micros=total_micros,
        out=rows,
    )
    height = NODE_HEIGHT * (depth_reached + 1) + 4
    # What the budget cut, counted from the payload rather than guessed, and stated below.
    drawn = len(rows)
    total_nodes = _count(tree)

    svg = "".join(
        [
            svg_open(
                CHART_WIDTH,
                height,
                label=f"{title}. Each row is one level of the folder tree; width is cost.",
            ),
            hatch_defs(chart_id),
            "".join(rows),
            "</svg>",
        ]
    )
    # The breadcrumb is rendered empty and filled by the script. Without scripting the chart is
    # the same complete, honest drawing it always was — zooming is an enhancement on top, not
    # the way the data is reached (FR-032).
    controls = (
        '<p class="flame-crumbs js-only" data-flame-crumbs>'
        '<button type="button" class="flame-crumb" data-flame-reset>All</button></p>'
    )
    note = (
        f"{note} Leaves are files. Click anything to zoom into it; the crumbs above the chart "
        f"lead back out, and zooming changes the scale, never a figure."
    ).strip()
    if drawn < total_nodes:
        note += (
            f" {total_nodes - drawn} of {total_nodes} nodes are below the drawing's depth "
            f"budget and are not shown here; they are in the table above and in every total."
        )
    return figure(chart_id=chart_id, title=title, svg=controls + svg, note=note)


def _count(node: Mapping[str, Any]) -> int:
    """Every node in the payload's tree, so an omission can be stated as a number."""
    return 1 + sum(_count(child) for child in node.get("children") or ())


def _draw(
    *,
    node: Mapping[str, Any],
    chart_id: str,
    x: int,
    width: int,
    span: tuple[float, float],
    depth: int,
    total_micros: int,
    out: list[str],
) -> int:
    """Emit one node and its children, returning the deepest level actually drawn.

    Children partition the parent's width exactly, with the parent's own cost taking the last
    block: the parts of a node are its children plus what it cost by itself, and those are the
    whole of it (FR-012).
    """
    out.append(_node_mark(node=node, chart_id=chart_id, x=x, width=width, span=span, depth=depth))
    children: Sequence[Mapping[str, Any]] = node.get("children") or ()
    # No `width <= 0` guard. A node whose pixel width rounds to zero at this scale is exactly
    # the node a zoom exists to reach, and dropping it left 198 of 372 unreachable — the chart
    # could not show the files under a small folder however far you zoomed. Pixels quantise;
    # the fractional span does not, so descent is bounded by depth and the node budget only.
    if not children or depth + 1 >= MAX_DEPTH or len(out) >= MAX_NODES:
        return depth

    flat = max(0, int(node.get("flat_micros", 0)))
    weights = [max(0, int(child.get("total_micros", 0))) for child in children] + [flat]
    widths = partition(width, weights)
    # The exact split, in fractions of the root. Pixels are what the reader sees now; these are
    # what a zoom re-lays-out from, and they survive a node being one pixel wide.
    span_total = sum(weights) or 1
    span_width = span[1] - span[0]
    edges = [span[0]]
    running = 0
    for weight in weights:
        running += weight
        edges.append(span[0] + span_width * running / span_total)
    deepest = depth
    cursor = x
    for index, (child, child_width) in enumerate(zip(children, widths[:-1], strict=True)):
        deepest = max(
            deepest,
            _draw(
                node=child,
                chart_id=chart_id,
                x=cursor,
                width=child_width,
                span=(edges[index], edges[index + 1]),
                depth=depth + 1,
                total_micros=total_micros,
                out=out,
            ),
        )
        cursor += child_width

    own_width = widths[-1]
    if own_width > 0:
        # The parent's own cost is a sibling of its children, not a silent part of the bar
        # above: without it the row below would be narrower than the row above with no stated
        # reason, which reads as cost having gone missing.
        share = flat / total_micros if total_micros else 0.0
        out.append(
            f'<g class="mark"><rect class="node node--own" x="{cursor}" '
            f'y="{(depth + 1) * NODE_HEIGHT}" width="{own_width}" height="{NODE_HEIGHT - 4}" '
            f'rx="2"></rect><title>{escape(node.get("name", "/"))} — cost of this folder '
            f"itself: {escape(money_share_text(flat, share, _sig_figs(node)))}</title></g>"
        )
        if own_width >= MIN_LABELLED_NODE:
            out.append(
                f'<text class="node-label" x="{cursor + 4}" '
                f'y="{(depth + 1) * NODE_HEIGHT + NODE_HEIGHT // 2}">{OWN_COST_LABEL}</text>'
            )
        deepest = max(deepest, depth + 1)
    return deepest


def _node_mark(
    *,
    node: Mapping[str, Any],
    chart_id: str,
    x: int,
    width: int,
    span: tuple[float, float],
    depth: int,
) -> str:
    name = str(node.get("name", "/"))
    total = int(node.get("total_micros", 0))
    share = float(node.get("share", 0.0))
    y = depth * NODE_HEIGHT
    opacity = DEPTH_OPACITY[min(depth, len(DEPTH_OPACITY) - 1)]
    remainder = name == UNATTRIBUTED_SWATCH or node.get("path") == UNATTRIBUTED_SWATCH
    css_class = "node node--remainder" if remainder else "node"
    # The remainder is textured, not merely a different shade: it is the one block a reader
    # must never mistake for a folder (FR-040, FR-042).
    fill = f' fill="{swatch_fill(chart_id, UNATTRIBUTED_SWATCH)}"' if remainder else ""

    # Normalised span of this node across the root, so a zoom can re-lay-out the drawing
    # without recomputing a single figure: focusing a node is a change of scale, and every
    # figure below is already rendered and stays exactly as it was.
    geometry = (
        f' data-x0="{span[0]:.9f}" data-x1="{span[1]:.9f}"'
        f' data-depth="{depth}" data-name="{escape(name)}"'
        f' data-path="{escape(str(node.get("path", name)))}"'
    )
    label_text = escape(truncate(name, max(1, width // PIXELS_PER_CHARACTER)))
    labelled = width >= MIN_LABELLED_NODE
    return (
        f'<g class="mark flame-node"{geometry} tabindex="0" role="button">'
        f'<rect class="{css_class}" x="{x}" y="{y}" width="{width}" '
        f'height="{NODE_HEIGHT - 4}" rx="2" fill-opacity="{opacity}"{fill}></rect>'
        f'<text class="node-label" x="{x + 4}" y="{y + NODE_HEIGHT // 2}"'
        f"{'' if labelled else ' visibility="hidden"'}>{label_text}</text>"
        # "everything beneath it" spelled out, because the same folder appears in the
        # `--by folder` table as *its own files only* — two correct figures twenty times apart,
        # and a reader who meets both without being told which is which concludes one is wrong.
        f"<title>{escape(name)} — {escape(money_share_text(total, share, _sig_figs(node)))}"
        f", everything beneath it"
        f"{_own_clause(node)}</title></g>"
    )


def _own_clause(node: Mapping[str, Any]) -> str:
    """What this folder cost by itself, where that is not the whole of it."""
    own = int(node.get("flat_micros", 0))
    total = int(node.get("total_micros", 0))
    if not total or own == total:
        return ""
    return f"; {format_micros(own, _sig_figs(node))} of that is the node itself"


def _sig_figs(node: Mapping[str, Any]) -> int:
    """Precision the node's confidence supports, defaulting to the coarsest (FR-095)."""
    return int(node.get("display_sig_figs", 2))
