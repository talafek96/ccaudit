"""`--explain` — show how a figure was derived, down to the records that produced it.

This is a feature, not a debug aid (constitution Principle VI). It is the surface a skeptic
uses, and the tool's whole purpose is settling arguments — a number nobody can check is worth
nothing for that.

The bar (SC-008, FR-015): a reader can trace any single figure back to the records that
produced it **without rerunning the tool**. So a trace names the component, the formula, the
inputs, the policy in effect, the basis and confidence, and the source records — enough to
redo the arithmetic by hand.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from ccaudit.analyse import SessionAnalysis
from ccaudit.config import attribution_component, sig_figs_for
from ccaudit.model.attribute import Attribution
from ccaudit.model.policy import describe as describe_policy
from ccaudit.money import format_micros, format_share


class UnknownFigureError(LookupError):
    """No figure by that identifier. Not an error state — the caller lists what is available."""


@dataclass
class Trace:
    """One figure's derivation, in the order a reader needs it."""

    figure_id: str
    headline: str
    lines: list[str] = field(default_factory=list)
    source_refs: tuple[str, ...] = ()

    def render(self) -> str:
        body = "\n".join(f"  {line}" for line in self.lines)
        refs = "\n".join(f"    {ref}" for ref in self.source_refs)
        out = f"{self.headline}\n{body}"
        if refs:
            out += f"\n\n  Source records:\n{refs}"
        return out


def figure_id(attribution: Attribution) -> str:
    """A stable handle a reader can copy out of a report and pass back to `explain`."""
    target = attribution.target_id or "session"
    return f"{attribution.component}:{target}"


def available_figures(analysis: SessionAnalysis) -> list[str]:
    """Every figure that can be explained, in a stable order."""
    return sorted({figure_id(a) for a in analysis.attribution.attributions})


def explain(analysis: SessionAnalysis, wanted: str) -> Trace:
    """Build the derivation trace for one figure.

    Matches on the exact figure id, then on a unique suffix — a reader should be able to type
    `explain src/app.py` rather than the full internal item id.
    """
    matches = [a for a in analysis.attribution.attributions if figure_id(a) == wanted]
    if not matches:
        matches = [
            a
            for a in analysis.attribution.attributions
            if wanted in figure_id(a) or (a.target_id and wanted in a.target_id)
        ]
    if not matches:
        raise UnknownFigureError(
            f"no figure matching {wanted!r} in session {analysis.session_id}. "
            f"Available: {', '.join(available_figures(analysis)[:12])}"
        )

    grouped = _group(matches)
    if len(grouped) > 1:
        raise UnknownFigureError(
            f"{wanted!r} matches {len(grouped)} figures: {', '.join(sorted(grouped))}. "
            f"Pass one of them."
        )

    key, rows = next(iter(grouped.items()))
    return _trace(analysis, key, rows)


def _remainder_lines(analysis: SessionAnalysis) -> list[str]:
    """Say which charge the remainder sits in, so a large one reads as a limit, not a bug.

    A reader who sees "33.7% couldn't be attributed" has no way to tell a broken tool from an
    honest refusal. Almost always it is the latter, and the evidence is which side it falls
    on: cache reads are re-shows of content already seen and named, so carry attributes
    nearly in full, while a cache write also pays for the resident instruction block — system
    prompt, tool and MCP schemas — that is stripped before the transcript is written. Cost we
    can see charged but whose content is provably absent cannot be tied to an item, and
    guessing would be the actual defect (FR-019).
    """
    charges = analysis.attribution.charges
    write_charged = sum(charge.cache_write_micros for charge in charges)
    if not write_charged:
        return []
    # 'direct' is exactly the per-item share of the write charge; the gap is the rest of it.
    direct_attributed = sum(
        row.cost_micros for row in analysis.attribution.attributions if row.component == "direct"
    )
    gap = write_charged - direct_attributed
    if gap <= 0:
        return []
    return [
        (
            f"Nearly all of that sits in loading into context: of the "
            f"{format_micros(write_charged, 2)} charged for cache writes,"
        ),
        (
            f"{format_micros(direct_attributed, 2)} was tied to a named item and "
            f"{format_micros(gap, 2)} ({format_share(gap / write_charged)} of the write charge)"
        ),
        "was not. A cache write also pays for the resident instruction block — system prompt,",
        "tool and MCP schemas — which is stripped before the transcript is written. That cost",
        "is visible as a charge but its content is absent, so it is reported here rather than",
        "guessed onto a file. More tools or MCP servers make this block, and this line, bigger.",
        "",
    ]


def explain_total(analysis: SessionAnalysis) -> Trace:
    """Explain the session total — the figure a reader challenges first."""
    reconciliation = analysis.reconciliation
    charges = analysis.attribution.charges
    lines = [
        f"Session {analysis.session_id} over {len(analysis.timeline.turns)} turn(s).",
        "",
        "The total is the sum of what each turn was charged, priced from the token counts",
        "recorded in the transcript. Nothing here is modelled or predicted:",
        "",
        f"  loading into context (cache write): {format_micros(sum(c.cache_write_micros for c in charges), 6)}",
        f"  keeping context loaded (cache read): {format_micros(sum(c.cache_read_micros for c in charges), 6)}",
        f"  your new typing (uncached input):    {format_micros(sum(c.fresh_input_micros for c in charges), 6)}",
        f"  what Claude wrote back (output):     {format_micros(sum(c.output_micros for c in charges), 6)}",
        "",
        f"  total: {format_micros(reconciliation.total_micros, 6)}",
        "",
        (
            f"Of that, {format_micros(reconciliation.attributed_micros, 4)} "
            f"({format_share(1 - reconciliation.unattributed_share)}) is attributed to specific"
        ),
        (
            f"items, and {format_micros(reconciliation.unattributed_micros, 2)} "
            f"({format_share(reconciliation.unattributed_share)}) could not be attributed."
        ),
        "",
        *_remainder_lines(analysis),
        f"Rates: {analysis.pricing.provenance}.",
        "This is an estimate of API-equivalent cost. It is not a bill.",
    ]
    lines.extend(["", "Limitations that affect these figures:"])
    lines.extend(f"  - {note}" for note in analysis.limitations)
    return Trace(
        figure_id="total", headline=f"Session total for {analysis.session_id}", lines=lines
    )


def _group(attributions: Iterable[Attribution]) -> dict[str, list[Attribution]]:
    grouped: dict[str, list[Attribution]] = {}
    for attribution in attributions:
        grouped.setdefault(figure_id(attribution), []).append(attribution)
    return grouped


def _trace(analysis: SessionAnalysis, key: str, rows: list[Attribution]) -> Trace:
    component = attribution_component(rows[0].component)
    total = sum(row.cost_micros for row in rows)
    session_total = analysis.reconciliation.total_micros
    share = total / session_total if session_total else 0.0
    confidence = _weakest_confidence(rows)
    sig_figs = sig_figs_for(confidence)

    target_id = rows[0].target_id
    item = analysis.timeline.items.get(target_id) if target_id else None

    lines = [
        f"Figure:     {format_micros(total, sig_figs)}  ({format_share(share)} of session total)",
        f"Component:  {component.plain_name} ({component.technical_name})",
        f"            {component.description}",
        f"Basis:      {rows[0].basis} — confidence {confidence}",
        f"            shown to {sig_figs} significant figures, which is what that confidence supports",
        "",
    ]

    if item is not None:
        lines.extend(
            [
                f"Item:       {item.identity}",
                (
                    f"            category {item.category}, measured at "
                    f"{item.size_tokens:,} tokens ({item.basis})"
                ),
                (
                    f"            loaded {analysis.timeline.load_count(item.item_id)} time(s), "
                    f"resident for {analysis.timeline.turns_resident(item.item_id)} turn(s)"
                ),
                "",
            ]
        )

    lines.extend(_component_derivation(analysis, rows, component.id))
    lines.extend(
        [
            "",
            f"Contributing turns: {len(rows)}",
        ]
    )
    for row in rows[:10]:
        lines.append(f"  turn {row.turn_index}: {format_micros(row.cost_micros, sig_figs)}")
    if len(rows) > 10:
        lines.append(f"  ... and {len(rows) - 10} more turn(s)")

    refs = tuple(dict.fromkeys(ref for row in rows for ref in row.source_refs))
    return Trace(figure_id=key, headline=f"How {key} was derived", lines=lines, source_refs=refs)


def _component_derivation(
    analysis: SessionAnalysis, rows: list[Attribution], component_id: str
) -> list[str]:
    """The formula, stated so it can be recomputed by hand."""
    if component_id == "carry":
        return [
            "Formula:    for each turn this item was resident, its share of that turn's",
            "            re-show charge, divided by the splitting policy:",
            f"            {describe_policy(analysis.policy)}",
            "",
            "            A carry figure therefore rests on a policy choice, not a measurement.",
            "            Changing the policy moves this number without changing the total.",
        ]
    if component_id == "direct":
        return [
            "Formula:    the turn's cache-write charge, divided among the items that arrived",
            "            on that turn in proportion to their size.",
            "",
            "            Cache-breakpoint placement decouples a write from the size of the",
            "            content that preceded it, so this is a best-effort join. Whatever it",
            "            does not explain stays in the unattributed remainder.",
        ]
    if component_id == "output":
        return [
            "Formula:    output tokens x the model's output rate.",
            "            Charged to the exchange, never to a file: what Claude wrote is caused",
            "            by the conversation, not by whichever file happened to be resident.",
        ]
    return [
        "Formula:    uncached input tokens x the model's input rate.",
        "            This is the conversation itself — prompts, replies, and scaffolding.",
    ]


def _weakest_confidence(rows: list[Attribution]) -> str:
    """A combined figure is only as trustworthy as its least trustworthy part."""
    order = {"low": 0, "medium": 1, "high": 2}
    return min((row.confidence for row in rows), key=lambda c: order[c])
