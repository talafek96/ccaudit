"""The shareable report — one self-contained HTML file, and the reason the tool exists.

The evidence is worth nothing if it cannot leave the machine that produced it. This file is
what goes to someone who will not install anything, will not run anything, will not read a
terminal, and may be disputing the conclusion. So:

**Nothing is fetched.** The stylesheet, the script, the charts, and the data are all inlined;
there is no ``<link>``, no font, no image, no analytics, and no URL of any kind in the output
(FR-032). Inline SVG needs no ``xmlns`` — the HTML parser puts it in the SVG namespace — which
is why the one URL that would otherwise be unavoidable is not here either.

**It reads correctly with JavaScript disabled.** Every figure, chart, and note is written into
the file at render time. The script sorts the table and overrides the theme; it computes
nothing, and the controls it drives are hidden until it runs, so the page never shows a dead
button.

**It carries the same wording the terminal does.** Same payload, same labels from the registry,
same ``format_micros`` at the same significant figures. A manager comparing the report against
a pasted terminal dump must not find two different numbers or two different names for a thing.

**It refuses to render a payload that does not add up.** The consumer invariant from
``contracts/report-data.md``: ``attributed + unattributed == cost_micros``, exact integer
equality, checked before a single tag is written.

Redaction is honoured structurally rather than by filtering at the end: only ``display`` is
ever rendered, and ``display`` is already the pseudonym when the payload is redacted (FR-043).
"""

import json
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any

from ccaudit.config.categories import MIXED_CATEGORY, tag_description
from ccaudit.config.components import attribution_component, sig_figs_for
from ccaudit.model.reconcile import UNATTRIBUTED_DISPLAY
from ccaudit.money import format_micros
from ccaudit.render.charts import (
    UNATTRIBUTED_SWATCH,
    Slice,
    money_share_html,
    placeholder,
    series_swatch,
)
from ccaudit.render.charts.bars import composition_bar, cumulative_sparkline, stacked_bars
from ccaudit.render.charts.hierarchy import icicle
from ccaudit.render.charts.scatter import cause_scatter, session_bars
from ccaudit.render.charts.timeline import residency_timeline
from ccaudit.render.data import (
    GROUPING_DESCRIPTIONS,
    GROUPINGS,
    forced_reload_micros,
    summarise_ids,
    summarise_versions,
)

ASSETS = Path(__file__).parent / "assets"
STYLESHEET = ASSETS / "report.css"
SCRIPT = ASSETS / "report.js"

REPORT_TITLE = "Where this session's money went"
COST_BASIS_SENTENCE = (
    "Every figure here is an API-equivalent cost estimate: token counts recorded in the "
    "session priced at published list rates. These are not billed amounts, and no invoice was "
    "consulted to produce them."
)
RECONCILES_SENTENCE = (
    "Every row plus the unattributed remainder adds up to the total exactly. Nothing is "
    "dropped, and nothing is spread across the items to make the table look tidy."
)

# How many items get their own bar and their own row before the rest are summed into one
# labelled line. The line is always present when it applies, so truncation hides rows and
# never cost.
TOP_ITEMS = 12

# How many further rows one "Show more" reveals. The rest stay collapsed, because the point of
# the truncation is that a 400-row table is unreadable, not that the rows are secret.
EXPAND_STEP = 25

_TRUNCATION_LABEL = "other items (not shown)"

# The same phrase appears in the chart and in the table, but only one of them can be expanded —
# a bar chart cannot grow a button, and a reader who found the phrase in the chart first went
# looking for one that was never there. So the chart's copy says where the control is.
# Short enough to survive the label gutter without being truncated itself.
_CHART_TRUNCATION_LABEL = "other items — see table"
_UNATTRIBUTED_NOTE = "cost we could not tie to any item"


def write_report(data: Mapping[str, Any], path: Path) -> Path:
    """Render the payload and write it to ``path``, returning the path written.

    Creates the parent directory if it does not exist. The file is written whole, in one call,
    so an interrupted run leaves no half-written report that reads as complete.
    """
    html = render_report_html(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def render_report_html(data: Mapping[str, Any]) -> str:
    """Render a report-data payload as one self-contained HTML document.

    Deterministic: the same payload produces the same bytes, on any machine and in any run
    (FR-017). The only clock reading in the output is ``generated_at``, which the payload
    carries and the caller can pin.

    Raises ``ValueError`` on a payload that does not add up — refusing to render is the
    contract for every consumer of this shape.
    """
    _refuse_if_it_does_not_add_up(data)

    body = "".join(
        [
            _header(data),
            _headline(data),
            _components_section(data),
            _items_section(data),
            _cause_section(data),
            _sessions_section(data),
            _hierarchy_section(data),
            _residency_section(data),
            _accumulation_section(data),
            _comparison_section(data),
            _notes_section(data),
            _footer(data),
        ]
    )
    return "".join(
        [
            "<!doctype html>\n",
            '<html lang="en">\n<head>\n',
            '<meta charset="utf-8">\n',
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
            f"<title>{escape(REPORT_TITLE)}</title>\n",
            f"<style>\n{STYLESHEET.read_text(encoding='utf-8')}</style>\n",
            "</head>\n<body>\n",
            f"<main>{body}</main>\n",
            _embedded_payload(data),
            f"<script>\n{SCRIPT.read_text(encoding='utf-8')}</script>\n",
            "</body>\n</html>\n",
        ]
    )


def _refuse_if_it_does_not_add_up(data: Mapping[str, Any]) -> None:
    """The consumer invariant, checked before anything is drawn (contracts/report-data.md).

    Stated again here rather than trusted from upstream because this file is the one artifact
    that leaves the machine: by the time it is read, nothing else in the pipeline is available
    to check it against.
    """
    totals = data["totals"]
    if totals["attributed_micros"] + totals["unattributed_micros"] != totals["cost_micros"]:
        raise ValueError(
            f"refusing to render a report whose breakdown does not add up: "
            f"{totals['attributed_micros']} + {totals['unattributed_micros']} != "
            f"{totals['cost_micros']}"
        )


# --- sections ---------------------------------------------------------------------------


def _header(data: Mapping[str, Any]) -> str:
    scope = data["scope"]
    sessions = scope.get("session_names") or scope["sessions_included"]
    parts = [
        f"<h1>{escape(REPORT_TITLE)}</h1>",
        f'<p class="lede">{escape(COST_BASIS_SENTENCE)}</p>',
        (
            '<div class="toolbar">'
            '<button type="button" class="theme-toggle js-only">Dark theme</button></div>'
        ),
        (
            f'<p class="meta">Sessions: {escape(summarise_ids(sessions)) or "none"}'
            f" · {scope['covered_through_turn']} turns"
            f" · carry split: {escape(str(data['policy']))}"
            f" · currency: {escape(str(data['currency']))}</p>"
        ),
    ]
    if scope["sessions_excluded_count"]:
        parts.append(
            f'<p class="meta">{scope["sessions_excluded_count"]} session(s) were excluded from '
            f"this result.</p>"
        )
    if scope.get("sessions_skipped"):
        skipped = scope["sessions_skipped"]
        parts.append(
            f'<p class="banner">{len(skipped)} session(s) could not be priced and are not in '
            f"these figures — they ran on a model this rate table does not cover: "
            f"{escape(summarise_ids(skipped))}</p>"
        )
    if len(scope["producing_versions"]) > 1:
        parts.append(
            f'<p class="meta">Spans Claude Code versions '
            f"{escape(summarise_versions(scope['producing_versions']))}; figures may not be comparable "
            f"across the boundary.</p>"
        )
    if scope["provisional"]:
        parts.append(
            '<p class="banner"><strong>Provisional.</strong> This session may still be running, '
            "so the most recent activity may not be included.</p>"
        )
    if data["redacted"]:
        parts.append(
            '<p class="banner"><strong>Paths are redacted.</strong> File names are replaced by '
            "stable pseudonyms; costs, shares, and structure are unchanged.</p>"
        )
    return "".join(parts)


def _headline(data: Mapping[str, Any]) -> str:
    totals = data["totals"]
    figures = totals["display_sig_figs"]
    total = totals["cost_micros"]
    panel = [
        '<section class="panel">',
        "<h2>The total</h2>",
        (
            f'<p class="hero"><span class="money">{escape(format_micros(total, figures))}</span> '
            f'<span class="share">(100.0% of total)</span></p>'
        ),
        '<p class="meta">API-equivalent cost estimate — these are not billed amounts.</p>',
        (
            "<p>Accounted for: "
            f"{money_share_html(totals['attributed_micros'], totals['attributed_share'], figures)}"
            f"<br>{escape(UNATTRIBUTED_DISPLAY.capitalize())} "
            f"({escape(_UNATTRIBUTED_NOTE)}): "
            f"{money_share_html(totals['unattributed_micros'], totals['unattributed_share'], figures)}"
            "</p>"
        ),
        f'<p class="reconciles">{escape(RECONCILES_SENTENCE)}</p>',
        "</section>",
    ]
    chart = composition_bar(
        chart_id="composition",
        title="What the money was concluded to be for",
        slices=_attribution_slices(data),
        total_micros=total,
        note=(
            "Every conclusion, plus the part we could not tie to anything, side by side. The "
            "bar is the whole session."
        ),
    )
    return "".join(panel) + chart


def _attribution_slices(data: Mapping[str, Any]) -> list[Slice]:
    """The four attribution conclusions plus the remainder — the parts of the session total.

    The remainder is a slice like any other, always, however large it is (FR-040). A remainder
    that looks bad is still the finding.
    """
    totals = data["totals"]
    slices = [
        Slice(
            label=f"{component['plain_name']} ({component['technical_name']})",
            micros=component["cost_micros"],
            share=component["share"],
            sig_figs=component["display_sig_figs"],
            swatch=series_swatch(index),
            detail=component["description"],
        )
        for index, component in enumerate(data["attribution"])
    ]
    slices.append(
        Slice(
            label=UNATTRIBUTED_DISPLAY,
            micros=totals["unattributed_micros"],
            share=totals["unattributed_share"],
            sig_figs=totals["display_sig_figs"],
            swatch=UNATTRIBUTED_SWATCH,
            detail=_UNATTRIBUTED_NOTE,
        )
    )
    return slices


def _components_section(data: Mapping[str, Any]) -> str:
    """How the cost was *incurred* — the four charge lanes, which are the whole total."""
    total = data["totals"]["cost_micros"]
    slices = [
        Slice(
            label=f"{component['plain_name']} ({component['technical_name']})",
            micros=component["cost_micros"],
            share=component["share"],
            sig_figs=component["display_sig_figs"],
            swatch=series_swatch(index),
            detail=f"{component['description']} {component['tokens']:,} tokens.",
        )
        for index, component in enumerate(data["components"])
    ]
    chart = composition_bar(
        chart_id="components",
        title="The four charge lanes, priced",
        slices=slices,
        total_micros=total,
        note=(
            "These four lanes are the whole session total, so the unattributed remainder is not "
            "one of them — it appears in the breakdown above, which divides the same total by "
            "what the cost was for."
        ),
    )
    return f"<h2>How the cost was incurred</h2>{chart}"


def _items_section(data: Mapping[str, Any]) -> str:
    """The ranking, rendered once per grouping so the choice is local to this section.

    Every dimension is drawn server-side and all but the chosen one are hidden. The alternative
    — rebuilding rows in the browser — would put a second row renderer in JavaScript, and the
    figures it drew would no longer be the ones Python reconciled (Principle IX, Principle X).
    Hiding costs bytes; recomputing costs trust.
    """
    active = str(data["group_by"])
    by_grouping: Mapping[str, Sequence[Mapping[str, Any]]] = data.get("items_by_grouping") or {
        active: data["items"]
    }
    views = "".join(
        f'<div class="grouped" data-grouping="{escape(name)}"'
        f"{'' if name == active else ' hidden'}>"
        f"{_items_view(data, by_grouping[name], name)}</div>"
        for name in by_grouping
    )
    return "".join(
        [
            "<h2>What cost the most</h2>",
            section_controls(_grouping_switch(by_grouping, active), filterable=True),
            views,
        ]
    )


def section_controls(*extra: str, filterable: bool = False) -> str:
    """The controls for one section, rendered beside the thing they change.

    They live here rather than in a global panel because a global "group by" or "tag filter"
    changes several sections at once and reads as a page-wide mode, when what a reader wants is
    to regroup *this* table or hide *these* rows. The tag list is filled in by the script from
    the rows actually present in this section — a server-rendered list would be a second source
    of truth that could offer a tag the section does not have.
    """
    parts = list(extra)
    if filterable:
        parts.append(
            '<label class="section-control js-only"><span>Filter</span>'
            '<input type="search" class="row-filter" placeholder="part of a name"></label>'
        )
        parts.append('<div class="tag-filter js-only" data-tag-filter hidden></div>')
        parts.append('<span class="filter-count js-only" data-filter-count></span>')
    return f'<div class="section-controls" data-section-controls>{"".join(parts)}</div>'


def _grouping_switch(by_grouping: Mapping[str, Any], active: str) -> str:
    """The per-section regroup control. Inert without scripting, so it is hidden until it works."""
    options = "".join(
        f'<option value="{escape(name)}" title="{escape(GROUPING_DESCRIPTIONS.get(name, ""))}"'
        f"{' selected' if name == active else ''}>"
        f"{escape(name)} ({len(by_grouping[name])})</option>"
        for name in by_grouping
    )
    # The description of the *chosen* dimension is shown, not only offered on hover: a reader
    # who has to hover each option to find out what it does has been handed a puzzle.
    meanings = "".join(
        f'<span class="grouping-meaning" data-grouping="{escape(name)}"'
        f"{'' if name == active else ' hidden'}>{escape(GROUPING_DESCRIPTIONS.get(name, ''))}"
        "</span>"
        for name in by_grouping
    )
    return (
        '<label class="section-control js-only"><span>Group these rows by</span>'
        f'<select class="regroup" data-target="items">{options}</select></label>'
        f'<p class="section-note js-only">{meanings}</p>'
    )


def _items_view(data: Mapping[str, Any], items: Sequence[Mapping[str, Any]], group_by: str) -> str:
    totals = data["totals"]
    total = totals["cost_micros"]
    shown = list(items[:TOP_ITEMS])
    omitted = list(items[TOP_ITEMS:])

    direct = attribution_component("direct")
    carry = attribution_component("carry")
    direct_label = f"{direct.plain_name} ({direct.technical_name})"
    carry_label = f"{carry.plain_name} ({carry.technical_name})"

    rows: list[tuple[str, Sequence[Slice]]] = [
        (
            str(item["display"]),
            [
                Slice(
                    label=direct_label,
                    micros=item["direct_micros"],
                    share=_share(item["direct_micros"], total),
                    sig_figs=item["display_sig_figs"],
                    swatch=series_swatch(0),
                ),
                Slice(
                    label=carry_label,
                    micros=item["carry_micros"],
                    share=_share(item["carry_micros"], total),
                    sig_figs=item["display_sig_figs"],
                    swatch=series_swatch(1),
                ),
            ],
        )
        for item in shown
    ]
    if omitted:
        omitted_micros = sum(item["total_micros"] for item in omitted)
        rows.append(
            (
                f"{len(omitted)} {_CHART_TRUNCATION_LABEL}",
                [
                    Slice(
                        label=_CHART_TRUNCATION_LABEL,
                        micros=omitted_micros,
                        share=_share(omitted_micros, total),
                        sig_figs=min(item["display_sig_figs"] for item in omitted),
                        swatch=series_swatch(1),
                    )
                ],
            )
        )
    for index, component in enumerate(data["attribution"]):
        if component["per_item"]:
            continue
        rows.append(
            (
                f"{component['plain_name']}",
                [
                    Slice(
                        label=f"{component['plain_name']} ({component['technical_name']})",
                        micros=component["cost_micros"],
                        share=component["share"],
                        sig_figs=component["display_sig_figs"],
                        swatch=series_swatch(index),
                    )
                ],
            )
        )
    rows.append(
        (
            UNATTRIBUTED_DISPLAY,
            [
                Slice(
                    label=UNATTRIBUTED_DISPLAY,
                    micros=totals["unattributed_micros"],
                    share=totals["unattributed_share"],
                    sig_figs=totals["display_sig_figs"],
                    swatch=UNATTRIBUTED_SWATCH,
                )
            ],
        )
    )

    chart = stacked_bars(
        chart_id="items",
        title="What cost the most, and whether it was the loading or the keeping",
        rows=rows,
        # No legend: this chart sits under two sections that already carry the same one, and a
        # third copy is noise between the reader and the ranking.
        legend=(),
        total_micros=total,
        ranked=len(shown),
        note=(
            "The item bars share one scale, set by the most expensive item. The rows beneath "
            "them are sums — the items not shown, cost the exchange itself caused rather than "
            "any file, and the remainder — so one of them can exceed every item and is drawn "
            "at full width, cut short with a broken-axis mark rather than being allowed to "
            "shrink the ranking above it. Together they reach the session total. The items not "
            "shown here are listed in the table below, which has a button to reveal them."
        ),
    )
    return "".join(
        [
            (
                f'<p class="lede">Rows are grouped by <strong>{escape(group_by)}'
                f"</strong>. The terminal and the browser view can group by "
                f"{escape(', '.join(GROUPINGS))} — a grouping only ever merges rows, so every "
                f"one of them sums to the same total.{escape(_grouping_caveat(group_by))}</p>"
            ),
            chart,
            _items_table(data, shown=shown, omitted=omitted),
            _cacheability(shown),
        ]
    )


def _grouping_caveat(group_by: str) -> str:
    """The sentence that stops two correct figures from looking like a contradiction.

    A folder row here is the files sitting *directly* in it; the same folder in the tree chart
    is everything beneath it. Rolling every file into all of its ancestors would count it many
    times over in one flat table, so both framings are needed — and each has to say which it is,
    or a reader meeting $14 in one place and $286 in the other concludes the tool is wrong.
    """
    if group_by != "folder":
        return ""
    return (
        " A folder row is the files sitting directly in it, not everything beneath it — "
        "otherwise a file would be counted once for every folder above it. The folder tree "
        "further down shows the everything-beneath-it figure."
    )


def _cause_section(data: Mapping[str, Any]) -> str:
    """The plot that shows why a read counter names the wrong files."""
    return (
        "<h2>Cost against read count</h2>"
        '<p class="lede">Two files can cost the same for opposite reasons. Ranking by cost and '
        "ranking by how often something was read do not produce the same list, and this is "
        "where that becomes visible rather than merely stated.</p>" + cause_scatter(data["items"])
    )


def _sessions_section(data: Mapping[str, Any]) -> str:
    """Per-session bars — only where there is more than one session to compare."""
    chart = session_bars(data.get("sessions", ()))
    if not chart:
        return ""
    return "<h2>Session by session</h2>" + chart


def _items_table(
    data: Mapping[str, Any],
    *,
    shown: Sequence[Mapping[str, Any]],
    omitted: Sequence[Mapping[str, Any]],
) -> str:
    """The ranked table, sortable by every reported measure (FR-038).

    Sorting is a JavaScript enhancement over rows already ordered most-expensive-first, so a
    reader with scripting disabled still gets the ranking the report is about.
    """
    totals = data["totals"]
    total = totals["cost_micros"]
    direct = attribution_component("direct")
    carry = attribution_component("carry")

    headers = [
        ("Item", "name", "asc"),
        ("Size (tokens)", "size", "desc"),
        (
            f"{direct.plain_name}<br><span class='meta'>{direct.technical_name}</span>",
            "direct",
            "desc",
        ),
        (
            f"{carry.plain_name}<br><span class='meta'>{carry.technical_name}</span>",
            "carry",
            "desc",
        ),
        ("Estimated cost", "total", "desc"),
        ("Reads", "reads", "desc"),
        ("Turns resident", "turns", "desc"),
    ]
    head = "".join(
        f'<th scope="col">{header}'
        f'<button type="button" class="sort-btn js-only" data-sort-key="{key}" '
        f'data-sort-default="{default}" aria-pressed="false" '
        f'aria-label="Sort by {escape(key)}">↕</button></th>'
        for header, key, default in headers
    )

    body = "".join(_item_row(item, total) for item in shown)
    if omitted:
        # The hidden rows are rendered, not withheld: revealing one is a display change, never a
        # recomputation. Their figures were priced in Python like every other row.
        body += "".join(_item_row(item, total, overflow=True) for item in omitted)
        body += _remainder_row(omitted, total)
    body += _forced_reload_row(data)
    for component in data["attribution"]:
        if component["per_item"]:
            continue
        body += _summary_row(
            label=f"{component['plain_name']} ({component['technical_name']})",
            micros=component["cost_micros"],
            share=component["share"],
            sig_figs=component["display_sig_figs"],
        )
    body += _summary_row(
        label=UNATTRIBUTED_DISPLAY,
        micros=totals["unattributed_micros"],
        share=totals["unattributed_share"],
        sig_figs=totals["display_sig_figs"],
        css_class="summary remainder",
    )

    return (
        f'<div class="table-wrap"><table data-sortable="1">'
        f"<caption>{escape(RECONCILES_SENTENCE)}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _forced_reload_row(data: Mapping[str, Any]) -> str:
    micros = forced_reload_micros(data)
    if not micros:
        return ""
    events = data["invalidations"]
    total = data["totals"]["cost_micros"]
    return _summary_row(
        label=(
            f"Re-loading after a change (invalidation) — {len(events)} event(s)"
            if len(events) != 1
            else "Re-loading after a change (invalidation) — 1 event"
        ),
        micros=micros,
        share=_share(micros, total),
        sig_figs=min(sig_figs_for(str(event["confidence"])) for event in events),
    )


def _remainder_row(omitted: Sequence[Mapping[str, Any]], total: int) -> str:
    """The "N other items" line, plus every state it will pass through as rows are revealed.

    **Why the states are precomputed.** Revealing a row has to decrease this line by exactly
    that row's cost, or the table stops adding up the moment a reader clicks — and a breakdown
    that does not reconcile is a show-stopper (Principle X, invariant A1). Doing that
    subtraction in JavaScript would mean a second implementation of ``format_micros`` and its
    significant-figure rule living in a language where money is a float. So every state is
    rendered here, in Python, from the same primitives as every other figure, and the script
    only swaps between strings it was handed. It cannot compute a figure, so it cannot compute
    a wrong one.
    """
    states = []
    for revealed in range(0, len(omitted), EXPAND_STEP):
        rest = omitted[revealed:]
        micros = sum(item["total_micros"] for item in rest)
        states.append(
            {
                "count": len(rest),
                "micros": micros,
                "label": f"{len(rest)} {_TRUNCATION_LABEL}",
                "figure": money_share_html(
                    micros,
                    _share(micros, total),
                    min(item["display_sig_figs"] for item in rest),
                ),
            }
        )
    micros = sum(item["total_micros"] for item in omitted)
    payload = escape(json.dumps(states), quote=True)
    button = (
        f'<button type="button" class="expand-btn js-only" data-expand-step="{EXPAND_STEP}" '
        f'data-expand-states="{payload}">Show {min(EXPAND_STEP, len(omitted))} more</button>'
    )
    return (
        f'<tr class="summary truncation" data-pinned="1" data-name="{escape(states[0]["label"])}" '
        f'data-total="{micros}">'
        f'<td><span class="expand-label">{escape(states[0]["label"])}</span> {button}</td>'
        f"<td></td><td></td><td></td>"
        f'<td class="num">{states[0]["figure"]}</td>'
        f"<td></td><td></td></tr>"
    )


def _what_it_is(item: Mapping[str, Any]) -> str:
    """What a pathless item actually is, said on the row.

    Most rows name a file the reader can open. A few name content Claude Code injects, and for
    those the row is the only place the reader can learn what they are looking at — "Skill
    listing" is a better name than `skill_listing`, but it still does not say that the listing
    is the *menu* of skills rather than the skills themselves.
    """
    what = str(item.get("what_it_is") or "")
    if not what:
        return ""
    return f'<p class="meta what-it-is">{escape(what)}</p>'


def flag(label: str, tag: str, **values: str) -> str:
    """One tag, carrying its own explanation and the key a reader can filter by.

    A tag is a compression — "too small to cache on claude-opus-5" is four facts in six words —
    and a compression the reader cannot expand is jargon, which Principle X calls a defect. The
    sentence comes from the one registry in `config/categories.py`; `data-tip` is what the
    page's balloon reads, and the native `title` is the fallback for a reader with no scripting.
    """
    tip = tag_description(tag, **values)
    return (
        f' <span class="flag" data-tag="{escape(tag)}" data-tip="{escape(tip)}" '
        f'title="{escape(tip)}" tabindex="0">{escape(label)}</span>'
    )


def _item_tags(item: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every tag on an item's row, as (label, key) — the one place the set is decided."""
    category = str(item["category"])
    tags = [(category, "mixed" if category == MIXED_CATEGORY else category)]
    if item["never_cacheable_on"]:
        models = ", ".join(item["never_cacheable_on"])
        tags.append((f"too small to cache on {models}", "uncacheable"))
    return tags


def _item_row(item: Mapping[str, Any], total: int, *, overflow: bool = False) -> str:
    figures = item["display_sig_figs"]
    display = str(item["display"])
    tags = _item_tags(item)
    flags = "".join(
        flag(
            label,
            key,
            model=", ".join(item["never_cacheable_on"]),
        )
        for label, key in tags
    )
    # `hidden` and not a CSS class: a reader with scripting off sees the same twelve rows and
    # the same remainder line, so the static page is still complete and still reconciles.
    marker = ' data-overflow="1" hidden' if overflow else ""
    return (
        f'<tr{marker} data-name="{escape(display)}" data-size="{item["size_tokens"]}" '
        f'data-direct="{item["direct_micros"]}" data-carry="{item["carry_micros"]}" '
        f'data-total="{item["total_micros"]}" data-reads="{item["reads"]}" '
        # The filter matches against this, so a reader can ask for every uncacheable row the
        # same way they ask for every row whose name contains "spec".
        f'data-tags="{escape(" ".join(key for _, key in tags))}" '
        f'data-turns="{item["turns_resident"]}">'
        f"<td>{escape(display)}{flags}"
        f"{_what_it_is(item)}{_parts_details(item)}{_drilldown(item, total)}</td>"
        f'<td class="num">{item["size_tokens"]:,}</td>'
        f'<td class="num">'
        f"{money_share_html(item['direct_micros'], _share(item['direct_micros'], total), figures, of_total=False)}</td>"
        f'<td class="num">'
        f"{money_share_html(item['carry_micros'], _share(item['carry_micros'], total), figures, of_total=False)}</td>"
        f'<td class="num">{money_share_html(item["total_micros"], item["share"], figures)}</td>'
        f'<td class="num">{item["reads"]:,}</td>'
        f'<td class="num">{item["turns_resident"]:,}</td>'
        f"</tr>"
    )


def _parts_details(item: Mapping[str, Any]) -> str:
    """What a composite item is made of — for the skill catalogue, the skills.

    "Skills: $67" is not an answer anyone can act on. This says which, and which of them came
    with a plugin: those are not the reader's to edit, so they are the part of the bill that
    changes by uninstalling something rather than by writing less.
    """
    figures = int(item["display_sig_figs"])
    parts = item.get("parts") or []
    if not parts:
        return ""
    plugin_micros = sum(int(p["cost_micros"]) for p in parts if p["origin"] == "plugin")
    rows = "".join(
        f"<li>{escape(str(part['name']))} — "
        f"{money_share_html(int(part['cost_micros']), float(part['share_of_item']), figures, of_total=False)}"
        f"{flag('from plugin ' + str(part['plugin']), 'plugin', plugin=str(part['plugin'])) if part['origin'] == 'plugin' else ''}"
        f"</li>"
        for part in parts
    )
    summary = f"{len(parts)} skills in this listing" + (
        f"; {format_micros(plugin_micros, figures)} of it comes from installed plugins"
        if plugin_micros
        else "; all of them are yours"
    )
    return (
        f'<details class="drill drill--parts"><summary>{escape(summary)}</summary>'
        f"<ul>{rows}</ul>"
        f'<p class="meta">A skill\'s share is its share of the listing text. The listing is '
        f"cached as one block, so this divides that block's cost and does not change how any "
        f"of it was priced.</p>"
        f"</details>"
    )


def _drilldown(item: Mapping[str, Any], total: int) -> str:
    """Where this figure came from and how much to trust it (FR-014, FR-015, FR-096).

    A ``<details>`` element rather than a scripted panel: drill-down that works with scripting
    disabled, in every browser, including the odd one the report gets opened in.
    """
    figures = item["display_sig_figs"]
    uncertainty = item["uncertainty"]
    lanes = item["lanes"]

    def figure_html(micros: int) -> str:
        return money_share_html(micros, _share(micros, total), figures, of_total=False)

    lines = [
        (
            f"<li>Basis: {escape(str(item['basis']))}; confidence: "
            f"{escape(str(item['confidence']))}, so figures are shown to {figures} significant "
            f"figure(s).</li>"
        ),
        (
            f"<li>Range, driven by {escape(str(uncertainty['driver']))}: "
            f"{figure_html(uncertainty['low_micros'])} to "
            f"{figure_html(uncertainty['high_micros'])}.</li>"
        ),
        (
            f"<li>Kept at the cache rate: {figure_html(lanes['cached_micros'])}"
            f"; loaded into context: {figure_html(lanes['loading_micros'])}"
            f"; charged at full rate: {figure_html(lanes['uncached_micros'])}.</li>"
        ),
    ]
    for entry in item["per_session"]:
        lines.append(
            f"<li>Session {escape(str(entry['session_id']))}: "
            f"{figure_html(entry['total_micros'])}.</li>"
        )
    return (
        f'<details class="drill"><summary>Where this figure comes from</summary>'
        f"<ul>{''.join(lines)}</ul></details>"
    )


def _summary_row(
    *,
    label: str,
    micros: int,
    share: float,
    sig_figs: int,
    css_class: str = "summary",
) -> str:
    """A row carrying a total but no per-item breakdown — never charged to a file."""
    return (
        f'<tr class="{css_class}" data-pinned="1" data-name="{escape(label)}" '
        f'data-total="{micros}">'
        f"<td>{escape(label)}</td><td></td><td></td><td></td>"
        f'<td class="num">{money_share_html(micros, share, sig_figs)}</td>'
        f"<td></td><td></td></tr>"
    )


def _cacheability(items: Sequence[Mapping[str, Any]]) -> str:
    """Items too small to cache — a ~10x per-turn difference, surfaced not buried (FR-078)."""
    flagged = [item for item in items if item["never_cacheable_on"]]
    if not flagged:
        return ""
    entries = "".join(
        f"<li>{escape(str(item['display']))} — {item['size_tokens']:,} tokens, below the "
        f"minimum on {escape(', '.join(item['never_cacheable_on']))}</li>"
        for item in flagged
    )
    return (
        "<h3>Too small to cache</h3>"
        "<p>These are charged at full rate every turn rather than at the cache rate.</p>"
        f"<ul>{entries}</ul>"
    )


def _hierarchy_section(data: Mapping[str, Any]) -> str:
    chart = icicle(
        chart_id="hierarchy",
        title="Folders by cost, deepest level last",
        tree=data["tree"],
        total_micros=data["totals"]["cost_micros"],
        note=(
            "Each row is one level of the tree. A block's width is everything below it; the "
            "block marked 'own' is what that folder cost by itself."
        ),
    )
    return f"<h2>Cost over the folder tree</h2>{chart}"


def _residency_section(data: Mapping[str, Any]) -> str:
    chart = residency_timeline(
        chart_id="residency",
        title="Residency by turn, one bar per span",
        spans=data["residency"],
        turn_count=max(1, int(data["scope"]["covered_through_turn"])),
        note=(
            "Length is turns, not time: content is charged for every turn it remains in the "
            "conversation, however long the gap between turns was."
        ),
    )
    return f"<h2>How long each item stayed in context</h2>{chart}"


def _accumulation_section(data: Mapping[str, Any]) -> str:
    totals = data["totals"]
    chart = cumulative_sparkline(
        chart_id="accumulation",
        title="Cumulative cost by turn",
        turns=data["turns"],
        total_micros=totals["cost_micros"],
        sig_figs=totals["display_sig_figs"],
        note="Compaction events are marked: they change what every following turn costs.",
    )
    return f"<h2>Cost as the session went on</h2>{chart}"


def _comparison_section(data: Mapping[str, Any]) -> str:
    """Always-present content against work-driven reads, on one scale (FR-037)."""
    comparison = data["comparison"]
    if not comparison:
        chart = placeholder(
            chart_id="comparison",
            title="Resident instructions against work-driven reads",
            reason=(
                "The split between resident instruction content and work-driven file reads is "
                "not yet computed."
            ),
        )
        return f"<h2>Always-present content versus files you read</h2>{chart}"

    total = data["totals"]["cost_micros"]
    rows: list[tuple[str, Sequence[Slice]]] = []
    legend: list[Slice] = []
    for index, key in enumerate(("resident_instruction", "work_driven_reads")):
        swatch = series_swatch(index)
        group = comparison.get(key, [])
        legend.append(
            Slice(
                label=key.replace("_", " "),
                micros=sum(entry["cost_micros"] for entry in group),
                share=sum(entry["share"] for entry in group),
                sig_figs=data["totals"]["display_sig_figs"],
                swatch=swatch,
            )
        )
        for entry in group:
            rows.append(
                (
                    f"{entry['label']} ({key.replace('_', ' ')})",
                    [
                        Slice(
                            label=str(entry["label"]),
                            micros=entry["cost_micros"],
                            share=entry["share"],
                            sig_figs=data["totals"]["display_sig_figs"],
                            swatch=swatch,
                        )
                    ],
                )
            )
    chart = stacked_bars(
        chart_id="comparison",
        title="Resident instructions against work-driven reads",
        rows=rows,
        legend=legend,
        total_micros=total,
        note=str(comparison.get("note", "")),
    )
    return (
        f"<h2>Always-present content versus files you read</h2>{chart}"
        f"{_comparison_members(comparison, data)}"
    )


def _comparison_members(comparison: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    """Which items are behind each bar.

    "Instruction files: $6.68" beside an $86 "Skills" bar reads as *CLAUDE.md is not in here* —
    it is, and a label alone gives a reader no way to find that out. Every bar opens to the
    items it is made of, dearest first.
    """
    figures = int(data["totals"]["display_sig_figs"])
    total = int(data["totals"]["cost_micros"])
    blocks: list[str] = []
    for key in ("resident_instruction", "work_driven_reads", "unassigned"):
        for entry in comparison.get(key, []) or []:
            members = entry.get("members") or []
            if not members:
                continue
            rows = "".join(
                f"<li>{escape(str(member['name']))} \u2014 "
                + money_share_html(
                    int(member["cost_micros"]),
                    _share(int(member["cost_micros"]), total),
                    figures,
                    of_total=False,
                )
                + "</li>"
                for member in members
            )
            blocks.append(
                f'<details class="drill drill--parts"><summary>'
                f"{escape(str(entry['label']))} \u2014 {len(members)} item(s), "
                f"{escape(format_micros(int(entry['cost_micros']), figures))}"
                f"</summary><ul>{rows}</ul></details>"
            )
    if not blocks:
        return ""
    joined = "".join(blocks)
    return f'<div class="member-lists">{joined}</div>'


def _notes_section(data: Mapping[str, Any]) -> str:
    """The uncertainty notes and the limitations — required output, not a footnote (FR-018)."""
    printed = list(data["totals"]["uncertainty_notes"])
    parts = [
        "<h2>How to read these numbers</h2>",
        f"<ul>{''.join(f'<li>{escape(note)}</li>' for note in printed)}</ul>",
    ]
    diagnostics = data["diagnostics"]
    remaining = [note for note in diagnostics["limitations"] if note not in printed]
    if remaining:
        parts.append("<h3>What these figures do not cover</h3>")
        parts.append(f"<ul>{''.join(f'<li>{escape(note)}</li>' for note in remaining)}</ul>")
    counts = []
    if diagnostics["unparseable_records"]:
        counts.append(
            f"<li>{diagnostics['unparseable_records']} record(s) could not be parsed and are "
            f"excluded from every figure above.</li>"
        )
    if diagnostics["estimated_figures"]:
        counts.append(
            f"<li>{diagnostics['estimated_figures']} item figure(s) rest on an estimated size "
            f"rather than a recorded token count.</li>"
        )
    if counts:
        parts.append(f"<ul>{''.join(counts)}</ul>")
    return "".join(parts)


def _footer(data: Mapping[str, Any]) -> str:
    scope = data["scope"]
    return (
        "<footer><p>"
        f"Produced by ccaudit {escape(str(data['tool_version']))} "
        f"(report schema {escape(str(data['schema_version']))}, "
        f"cost basis {escape(str(data['cost_basis']))}) at "
        f"{escape(str(data['generated_at']))}. "
        f"Covers {len(scope['sessions_included'])} session(s) through turn "
        f"{scope['covered_through_turn']}. "
        "The full data behind every figure is embedded in this file, in the "
        "<code>ccaudit-data</code> block, so any number here can be checked without rerunning "
        "the tool."
        "</p></footer>"
    )


def _embedded_payload(data: Mapping[str, Any]) -> str:
    """The payload itself, inlined so every figure is reproducible by the reader (FR-015).

    ``<`` is escaped as a unicode sequence so no byte sequence in the data can close the script
    element early — the one way an embedded JSON literal turns into markup.

    Written compactly. Indentation would add about a third to the size of a file that has to
    survive being emailed, and it buys nothing: nobody reads this block in place — a reader
    checking a figure copies it out and formats it with their own tools.
    """
    literal = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="ccaudit-data">\n{literal}\n</script>\n'


def _share(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total
