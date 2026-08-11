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

MAX_DEPTH = 6
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
        depth=0,
        total_micros=total_micros,
        out=rows,
    )
    height = NODE_HEIGHT * (depth_reached + 1) + 4

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
    return figure(chart_id=chart_id, title=title, svg=svg, note=note)


def _draw(
    *,
    node: Mapping[str, Any],
    chart_id: str,
    x: int,
    width: int,
    depth: int,
    total_micros: int,
    out: list[str],
) -> int:
    """Emit one node and its children, returning the deepest level actually drawn.

    Children partition the parent's width exactly, with the parent's own cost taking the last
    block: the parts of a node are its children plus what it cost by itself, and those are the
    whole of it (FR-012).
    """
    out.append(_node_mark(node=node, chart_id=chart_id, x=x, width=width, depth=depth))
    children: Sequence[Mapping[str, Any]] = node.get("children") or ()
    if not children or depth + 1 >= MAX_DEPTH or width <= 0:
        return depth

    flat = max(0, int(node.get("flat_micros", 0)))
    weights = [max(0, int(child.get("total_micros", 0))) for child in children] + [flat]
    widths = partition(width, weights)
    deepest = depth
    cursor = x
    for child, child_width in zip(children, widths[:-1], strict=True):
        if child_width > 0:
            deepest = max(
                deepest,
                _draw(
                    node=child,
                    chart_id=chart_id,
                    x=cursor,
                    width=child_width,
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


def _node_mark(*, node: Mapping[str, Any], chart_id: str, x: int, width: int, depth: int) -> str:
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

    mark = (
        f'<g class="mark"><rect class="{css_class}" x="{x}" y="{y}" width="{width}" '
        f'height="{NODE_HEIGHT - 4}" rx="2" fill-opacity="{opacity}"{fill}></rect>'
        f"<title>{escape(name)} — {escape(money_share_text(total, share, _sig_figs(node)))}"
        f"</title></g>"
    )
    if width < MIN_LABELLED_NODE:
        return mark
    # A node's name is one path segment, so the identifying part is its head, not its tail.
    label = truncate(name, max(1, width // PIXELS_PER_CHARACTER), keep_tail=False)
    return (
        f'{mark}<text class="node-label" x="{x + 4}" y="{y + NODE_HEIGHT // 2}">'
        f"{escape(label)}</text>"
    )


def _sig_figs(node: Mapping[str, Any]) -> int:
    """Precision the node's confidence supports, defaulting to the coarsest (FR-095)."""
    return int(node.get("display_sig_figs", 2))
