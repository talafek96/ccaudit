"""The terminal surface — the primary and only mandatory interface.

Renders the report-data payload (:mod:`ccaudit.render.data`) and nothing else. It never
computes a figure, never re-types a label, and never formats money itself: every number comes
from the payload, every plain-language name from the registry that defines it, and every
string of dollars from :mod:`ccaudit.money`. That is what makes the terminal, the HTML report,
and the UI incapable of disagreeing (FR-074).

**Two modes, detected rather than guessed.** A real terminal gets tables, proportion bars, and
colour. Anything else — a pipe, a file, a captured subprocess — gets plain, stable-column text
with no escape sequences, because that output is going to be read by a script or pasted into a
ticket (FR-071).

**What both modes always carry**, with no flag able to switch it off:

- every figure labelled an API-equivalent cost estimate, never an amount charged (FR-010);
- every absolute paired with its share of the total (FR-011);
- the unattributed remainder on its own visible line, including when ``--top`` truncates the
  table — the omitted rows are summed into their own line rather than vanishing (FR-012/013);
- every figure at its ``display_sig_figs`` and never finer: a carry figure resting on a
  splitting policy is not printed to the cent (FR-095, FR-098);
- an in-progress session marked provisional (FR-067).

**Colour is never the only carrier of a distinction** (FR-042). Bars are drawn from glyphs
whose length is the signal, every emphasised row is also labelled in words, and the numeric
share sits beside every bar. Removing all colour removes nothing.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from rich import box
from rich.console import Console, JustifyMethod
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from ccaudit.config.components import attribution_component
from ccaudit.model.reconcile import UNATTRIBUTED_DISPLAY
from ccaudit.money import format_micros, format_share

# Fixed so captured output does not depend on the terminal that produced it (SC-009).
PLAIN_WIDTH = 120
BAR_WIDTH = 10
ITEM_COLUMN_WIDTH = 28
BAR_FILLED = "#"
BAR_EMPTY = "."

_UNATTRIBUTED_NOTE = "cost we could not tie to any item"
_TRUNCATION_LABEL = "other items (not shown)"


def render_report(
    data: Mapping[str, Any],
    *,
    console: Console | None = None,
    top: int | None = None,
) -> None:
    """Render a report-data payload to the console.

    ``console`` is injected so the caller — and the tests — decide where output goes; the
    default detects the real stream. ``top`` limits the item rows shown, never what is
    accounted for.

    Raises ``ValueError`` on a payload that does not add up: a consumer must refuse to render
    numbers that contradict their own total rather than display them (contracts/report-data.md).
    """
    target = console or build_console()
    _refuse_if_it_does_not_add_up(data)

    plain = not target.is_terminal
    _render_header(target, data, plain=plain)
    _render_totals(target, data, plain=plain)
    _render_components(target, data, plain=plain)
    _render_items(target, data, top=top, plain=plain)
    _render_notes(target, data)


def build_console(*, file: Any = None) -> Console:
    """A console that renders plainly when it is not attached to a terminal.

    Detection, not a guess: ``rich`` reports whether the stream is a TTY, and the non-TTY path
    fixes the width and strips colour so piped output is stable and free of escape sequences.
    """
    probe = Console(file=file)
    if probe.is_terminal:
        return probe
    return Console(file=file, width=PLAIN_WIDTH, no_color=True, highlight=False, soft_wrap=False)


def _refuse_if_it_does_not_add_up(data: Mapping[str, Any]) -> None:
    totals = data["totals"]
    if totals["attributed_micros"] + totals["unattributed_micros"] != totals["cost_micros"]:
        raise ValueError(
            f"refusing to render a breakdown that does not add up: "
            f"{totals['attributed_micros']} + {totals['unattributed_micros']} != "
            f"{totals['cost_micros']}"
        )


def _render_header(console: Console, data: Mapping[str, Any], *, plain: bool) -> None:
    scope = data["scope"]
    included = scope["sessions_included"]
    console.print(_styled("Where this session's money went", "bold", plain=plain))
    console.print(
        f"API-equivalent cost estimate — token counts priced at published list rates, "
        f"not an amount charged. Currency: {data['currency']}."
    )

    session_line = f"Sessions: {', '.join(included)}" if included else "Sessions: none"
    if scope["sessions_excluded_count"]:
        session_line += f"  ({scope['sessions_excluded_count']} excluded from this result)"
    console.print(
        f"{session_line}  ·  {scope['covered_through_turn']} turns  ·  "
        f"carry split: {data['policy']}"
    )
    if len(scope["producing_versions"]) > 1:
        console.print(
            f"Spans Claude Code versions {', '.join(scope['producing_versions'])}; "
            f"figures may not be comparable across the boundary."
        )
    if data["redacted"]:
        console.print("Paths are redacted. Costs, shares, and structure are unchanged.")
    if scope["provisional"]:
        # Words, not colour: this must survive being piped into a plain-text ticket (FR-042).
        console.print(
            _styled(
                "PROVISIONAL — this session may still be running, so the most recent "
                "activity may not be included.",
                "bold yellow",
                plain=plain,
            )
        )
    console.print()


def _render_totals(console: Console, data: Mapping[str, Any], *, plain: bool) -> None:
    totals = data["totals"]
    figures = totals["display_sig_figs"]
    console.print(
        _styled(
            f"Total (API-equivalent estimate): {format_micros(totals['cost_micros'], figures)}",
            "bold",
            plain=plain,
        )
    )
    label_width = len(UNATTRIBUTED_DISPLAY) + 2
    console.print(
        f"  {'accounted for:':<{label_width}} "
        f"{format_micros(totals['attributed_micros'], figures)}  "
        f"({format_share(totals['attributed_share'])} of total)"
    )
    console.print(
        f"  {UNATTRIBUTED_DISPLAY + ':':<{label_width}} "
        f"{format_micros(totals['unattributed_micros'], figures)}  "
        f"({format_share(totals['unattributed_share'])} of total)"
    )
    console.print()


def _render_components(console: Console, data: Mapping[str, Any], *, plain: bool) -> None:
    """Where the money went by cost component, in plain language with the term secondary."""
    table = _table(
        "How the cost was incurred",
        [
            ("Cost component", "left"),
            ("Estimated cost", "right"),
            ("Share", "right"),
            ("Tokens", "right"),
            ("Proportion", "left"),
        ],
        plain=plain,
    )
    for component in data["components"]:
        table.add_row(
            f"{component['plain_name']} ({component['technical_name']})",
            format_micros(component["cost_micros"], component["display_sig_figs"]),
            format_share(component["share"]),
            f"{component['tokens']:,}",
            _bar(component["share"]),
        )
    console.print(table)
    console.print()


def _render_items(
    console: Console, data: Mapping[str, Any], *, top: int | None, plain: bool
) -> None:
    """The leaderboard, with the omitted rows and the remainder both still on the page."""
    items: Sequence[Mapping[str, Any]] = data["items"]
    shown = items if top is None else items[:top]
    omitted = items[len(shown) :]

    direct = attribution_component("direct")
    carry = attribution_component("carry")
    table = _table(
        "What cost the most",
        [
            ("Item", "left"),
            (f"{direct.plain_name}\n({direct.technical_name})", "right"),
            (f"{carry.plain_name}\n({carry.technical_name})", "right"),
            ("Total", "right"),
            ("Share", "right"),
            ("Proportion", "left"),
            # "read 4 times, held for 58 turns" is the finding, so the two travel together.
            ("Reads /\nturns", "right"),
        ],
        plain=plain,
        first_column_width=ITEM_COLUMN_WIDTH,
    )

    for item in shown:
        figures = item["display_sig_figs"]
        table.add_row(
            _item_label(item),
            format_micros(item["direct_micros"], figures),
            format_micros(item["carry_micros"], figures),
            format_micros(item["total_micros"], figures),
            format_share(item["share"]),
            _bar(item["share"]),
            f"{item['reads']:,} / {item['turns_resident']:,}",
        )

    totals = data["totals"]
    if omitted:
        # Truncation hides rows, never cost. The omitted rows keep their own line so the
        # visible figures plus this line plus the remainder still equal the total.
        omitted_micros = sum(item["total_micros"] for item in omitted)
        omitted_share = _share(omitted_micros, totals["cost_micros"])
        omitted_figures = min(item["display_sig_figs"] for item in omitted)
        _add_summary_row(
            table,
            f"{len(omitted)} {_TRUNCATION_LABEL}",
            omitted_micros,
            omitted_share,
            omitted_figures,
        )

    # Cost the exchange caused rather than any file: the prompts, the scaffolding, and what
    # the model wrote back. Shown here so the column can be added up by hand and reach the
    # total (invariant A2 — output is never charged to a file).
    for component in data["attribution"]:
        if component["per_item"]:
            continue
        _add_summary_row(
            table,
            f"{component['plain_name']} ({component['technical_name']})",
            component["cost_micros"],
            component["share"],
            component["display_sig_figs"],
        )

    _add_summary_row(
        table,
        UNATTRIBUTED_DISPLAY,
        totals["unattributed_micros"],
        totals["unattributed_share"],
        totals["display_sig_figs"],
    )
    console.print(table)
    console.print(
        f"Every row above plus '{UNATTRIBUTED_DISPLAY}' ({_UNATTRIBUTED_NOTE}) adds up to the "
        f"total. Nothing is dropped or spread around."
    )
    console.print()
    _render_cacheability(console, shown)


def _add_summary_row(table: Table, label: str, micros: int, share: float, sig_figs: int) -> None:
    """A row that carries a total but no per-item breakdown — never charged to a file."""
    table.add_row(
        label, "", "", format_micros(micros, sig_figs), format_share(share), _bar(share), ""
    )


def _render_cacheability(console: Console, items: Sequence[Mapping[str, Any]]) -> None:
    """Surface items too small to cache — a ~10x per-turn difference, not a footnote (FR-078)."""
    flagged = [item for item in items if item["never_cacheable_on"]]
    if not flagged:
        return
    console.print("Too small to cache — charged at full rate every turn, not the cache rate:")
    for item in flagged:
        console.print(
            f"  {_item_label(item)} — {item['size_tokens']:,} tokens, below the minimum on "
            f"{', '.join(item['never_cacheable_on'])}"
        )
    console.print()


def _render_notes(console: Console, data: Mapping[str, Any]) -> None:
    """The uncertainty notes and the limitations. Required output, not garnish (FR-018)."""
    printed = list(data["totals"]["uncertainty_notes"])
    console.print("How to read these numbers:")
    for note in printed:
        console.print(f"  - {note}")

    diagnostics = data["diagnostics"]
    # The two sections overlap by design in the payload — each is self-contained for a JSON
    # consumer — but printing the same sentence twice reads as noise, so the second section
    # shows only what the first did not.
    limitations = [note for note in diagnostics["limitations"] if note not in printed]
    if limitations:
        console.print()
        console.print("What these figures do not cover:")
        for note in limitations:
            console.print(f"  - {note}")
    if diagnostics["unparseable_records"]:
        console.print(
            f"  - {diagnostics['unparseable_records']} record(s) could not be parsed and are "
            f"excluded from every figure above."
        )


def _item_label(item: Mapping[str, Any]) -> str:
    """An item's display name, with its category so the row reads without the legend."""
    # Escaped: a path is user data, and an unescaped "[...]" would be read as console markup
    # and silently disappear from the report.
    return escape(f"{item['display']} [{item['category']}]")


def _table(
    title: str,
    columns: Sequence[tuple[str, JustifyMethod]],
    *,
    plain: bool,
    first_column_width: int | None = None,
) -> Table:
    """A table that keeps its columns in a fixed order and alignment in both modes.

    The first column is given a floor width where the caller asks for one, so a long file path
    is not folded down to a couple of characters when the numeric columns claim the space.
    """
    table = Table(
        title=title,
        title_justify="left",
        box=None if plain else box.SIMPLE_HEAD,
        pad_edge=False,
        show_edge=not plain,
        header_style="" if plain else "bold",
    )
    for index, (header, justify) in enumerate(columns):
        table.add_column(
            header,
            justify=justify,
            overflow="fold",
            min_width=first_column_width if index == 0 else None,
        )
    return table


def _bar(share: float) -> str:
    """A proportion bar drawn from glyphs, so its length carries the signal, not its colour.

    Always accompanied by the numeric share in its own column (FR-011, FR-042).
    """
    filled = max(0, min(BAR_WIDTH, round(share * BAR_WIDTH)))
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_WIDTH - filled)


def _styled(text: str, style: str, *, plain: bool) -> Text:
    """Style only where a terminal can show it; plain mode gets the words unadorned."""
    return Text(text) if plain else Text(text, style=style)


def _share(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total
