"""The analysis pipeline: transcript in, reconciled breakdown out.

This is the composition root. It sits outside the ``config -> ingest -> model -> render``
layering for the same reason ``money`` does — something has to join the layers, and putting
the join inside any one of them would invert a dependency.

The order is fixed and each step earns its place:

1. **parse** — typed records, with unparseable ones counted rather than dropped.
2. **dedup** — before any arithmetic. Resume, fork, and compaction put the same exchange in
   the file more than once, and counting it twice inflates every figure downstream (FR-021).
3. **size** — the exact/measured/declared ladder. An image is sized from its header, never
   from character count.
4. **timeline** — what was resident, when, and until what.
5. **attribute** — split the observed charges across the items that caused them.
6. **reconcile** — check that the parts equal the whole, and raise if they do not.

**Analysis is a pure function of the transcript.** Same file, same figures, every run and
every machine (FR-017, SC-009). That is also what makes concurrency cheap: two analyses of the
same session race harmlessly, because they cannot disagree.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ccaudit.config import Pricing, load_pricing
from ccaudit.ingest.dedup import DedupResult, dedup_turns
from ccaudit.ingest.records import (
    AttachmentRecord,
    CompactionRecord,
    IngestDiagnostic,
    ParsedTranscript,
    ToolResultRecord,
    parse_transcript,
)
from ccaudit.ingest.tokens import (
    TokenQuantity,
    estimate_from_characters,
    resolve_tool_result_tokens,
)
from ccaudit.model.attribute import AttributionResult, attribute_session
from ccaudit.model.policy import DEFAULT_POLICY
from ccaudit.model.reconcile import Reconciliation, reconcile
from ccaudit.model.residency import Timeline, build_timeline

# Tool and MCP schema deltas report their size as line counts rather than as text. Anthropic
# does not publish a lines-to-tokens rate, so this is an explicit, declared estimate — the
# figure it produces is marked `estimated`/low and says so wherever it appears. It is here
# rather than buried at a call site because it is a tunable, and tunables live in one place.
TOKENS_PER_SCHEMA_LINE = 12


@dataclass
class SessionAnalysis:
    """One session's analysis, complete with what it could not account for."""

    session_id: str
    path: Path
    policy: str
    pricing: Pricing
    parsed: ParsedTranscript
    dedup: DedupResult
    timeline: Timeline
    attribution: AttributionResult
    reconciliation: Reconciliation
    limitations: list[str] = field(default_factory=list)

    @property
    def total_micros(self) -> int:
        return self.reconciliation.total_micros

    @property
    def diagnostics(self) -> dict[str, IngestDiagnostic]:
        merged = dict(self.parsed.diagnostics)
        merged.update(self.dedup.diagnostics)
        return merged

    @property
    def provisional(self) -> bool:
        """Whether this covers a session that may still be running (FR-067).

        Decided by the caller comparing coverage fingerprints; analysis itself has no way to
        know, and guessing from a timestamp would be a worse answer than asking.
        """
        return self._provisional

    _provisional: bool = False


def analyse_transcript(
    path: Path,
    *,
    pricing: Pricing | None = None,
    policy: str = DEFAULT_POLICY,
    project_path: str | None = None,
    provisional: bool = False,
) -> SessionAnalysis:
    """Run the whole pipeline over one transcript file.

    Raises :class:`~ccaudit.model.reconcile.ReconciliationError` if the breakdown does not add
    up — the tool refuses to hand back numbers that contradict their own total.
    """
    resolved_pricing = pricing or load_pricing()
    parsed = parse_transcript(path)
    deduped = dedup_turns([parsed])

    model_for_sizing = deduped.turns[0].model if deduped.turns else None
    timeline = build_timeline(
        deduped.turns,
        parsed.tool_results,
        parsed.attachments,
        parsed.compactions,
        sizer=_make_sizer(model_for_sizing),
        project_path=project_path,
    )

    session_id = parsed.session_id or path.stem
    attribution = attribute_session(
        session_id,
        timeline,
        resolved_pricing,
        policy=policy,
        # Attachments carry the tool-schema and skill-listing deltas that make a prefix
        # change observable; without them a forced reload has no detectable cause.
        attachments=parsed.attachments,
    )
    checked = reconcile(
        attribution.attributions,
        attribution.total_micros,
        scope=f"session {session_id}",
    )

    return SessionAnalysis(
        session_id=session_id,
        path=path,
        policy=policy,
        pricing=resolved_pricing,
        parsed=parsed,
        dedup=deduped,
        timeline=timeline,
        attribution=attribution,
        reconciliation=checked,
        limitations=_limitations(parsed, deduped, timeline, resolved_pricing),
        _provisional=provisional,
    )


def _make_sizer(model: str | None):
    """Bind the token ladder to the model in use, so image caps resolve per model."""

    def size(record: ToolResultRecord | AttachmentRecord) -> TokenQuantity:
        if isinstance(record, ToolResultRecord):
            return resolve_tool_result_tokens(record, model)
        return _size_attachment(record)

    return size


def _size_attachment(record: AttachmentRecord) -> TokenQuantity:
    """Size an attachment: its text where it has some, its line count where it does not."""
    if record.text_length:
        return estimate_from_characters(record.text_length, f"{record.attachment_type} attachment")

    added_lines = record.payload.get("addedLines")
    if isinstance(added_lines, int) and added_lines > 0:
        tokens = added_lines * TOKENS_PER_SCHEMA_LINE
        return TokenQuantity(
            tokens=tokens,
            basis="estimated",
            confidence="low",
            method=(
                f"{added_lines} schema lines x {TOKENS_PER_SCHEMA_LINE} tokens/line "
                f"(declared estimate: no published lines-to-tokens rate exists)"
            ),
        )
    return TokenQuantity(
        tokens=None,
        basis="estimated",
        confidence="low",
        method=f"{record.attachment_type} attachment carries no measurable size",
    )


def _limitations(
    parsed: ParsedTranscript,
    deduped: DedupResult,
    timeline: Timeline,
    pricing: Pricing,
) -> list[str]:
    """What the reader must be told alongside these figures (FR-018, FR-097).

    Required output, not garnish. Some of the disputed cost is provably absent from the source
    data, and a reader who is not told that will over-trust the breakdown.
    """
    notes = [
        (
            f"Costs are API-equivalent estimates imputed from the {pricing.provenance}. "
            f"They are not billed amounts."
        ),
        (
            "Some resident instruction content is stripped before the transcript is written, "
            "so a portion of the cost is provably absent from the source records."
        ),
    ]
    stale = pricing.staleness_note()
    if stale:
        notes.append(stale)
    if parsed.unparseable_count:
        notes.append(
            f"{parsed.unparseable_count} record(s) could not be parsed and are excluded from "
            f"every figure here."
        )
    if deduped.conflicts:
        notes.append(
            f"{len(deduped.conflicts)} exchange(s) appeared more than once with different token "
            f"counts; the first occurrence was used and the conflict is listed in diagnostics."
        )
    if timeline.unexplained_dropped_tokens:
        notes.append(
            f"About {timeline.unexplained_dropped_tokens:,} tokens left the conversation before "
            f"a compaction, which leaves no record of what was cleared. That content's carry "
            f"cost cannot be attributed to any item."
        )
    if parsed.spans_versions:
        notes.append(
            f"This session spans Claude Code versions "
            f"{', '.join(sorted(parsed.producing_versions))}; figures may not be comparable "
            f"across the boundary."
        )
    return notes


# --- the exportable conclusion -----------------------------------------------------------
#
# Everything below is what a finished analysis is worth keeping, and it lives here rather than
# in `store/` because it is not a storage concern: `render.data` consumes it, and putting it
# under the store would make the presentation layer depend on the cache.


@dataclass(frozen=True)
class ParsedFacts:
    """What the payload builder needs from the parse — never the records themselves."""

    producing_versions: set[str]
    compactions: list[CompactionRecord]
    unparseable_count: int


@dataclass(frozen=True)
class SessionContribution:
    """One session's finished conclusion, in the form that is stored and read back.

    Frozen because a cached value that can be mutated after restore is a cached value that can
    disagree with what was stored.
    """

    session_id: str
    policy: str
    provisional: bool
    parsed: ParsedFacts
    timeline: Timeline
    attribution: AttributionResult
    reconciliation: Reconciliation
    limitations: list[str] = field(default_factory=list)

    @property
    def total_micros(self) -> int:
        return self.reconciliation.total_micros

    def check_reconciles(self) -> None:
        """Invariant S2 — a restored result is checkable on its own terms.

        Re-derived from the stored parts rather than trusted. A cache that hands back a total
        nobody re-checked is a second source of truth, which is the thing this design exists to
        avoid — so the check is run on the way *out* of the store, not only on the way in.

        Note what the remainder is and is not: it is computed once by
        :func:`~ccaudit.model.reconcile.reconcile` from what the attributions leave unexplained,
        and is deliberately **not** an attribution row (passing it in as one would double-count
        it). So the check is that the rows account for ``attributed_micros`` exactly, and that
        the remainder makes up the rest of the total — never that a remainder row exists.
        """
        attributed = sum(row.cost_micros for row in self.attribution.attributions)
        observed = sum(charge.total_micros for charge in self.attribution.charges)
        if attributed != self.reconciliation.attributed_micros:
            raise ValueError(
                f"restored contribution for {self.session_id} does not reconcile: its "
                f"attributions sum to {attributed}, but it claims "
                f"{self.reconciliation.attributed_micros} attributed"
            )
        if attributed + self.reconciliation.unattributed_micros != observed:
            raise ValueError(
                f"restored contribution for {self.session_id} does not add up: "
                f"{attributed} + {self.reconciliation.unattributed_micros} != {observed}, the "
                f"total of the charges it stored"
            )
        if observed != self.reconciliation.total_micros:
            raise ValueError(
                f"restored contribution for {self.session_id} claims a total of "
                f"{self.reconciliation.total_micros}, but its stored charges sum to {observed}"
            )


def contribution_of(analysis: SessionAnalysis) -> SessionContribution:
    """Reduce a freshly-computed analysis to the part worth keeping."""
    return SessionContribution(
        session_id=analysis.session_id,
        policy=analysis.policy,
        provisional=analysis.provisional,
        parsed=ParsedFacts(
            producing_versions=set(analysis.parsed.producing_versions),
            compactions=list(analysis.parsed.compactions),
            unparseable_count=analysis.parsed.unparseable_count,
        ),
        timeline=analysis.timeline,
        attribution=analysis.attribution,
        reconciliation=analysis.reconciliation,
        limitations=list(analysis.limitations),
    )


# What every presentation surface accepts. Both a freshly-computed analysis and a contribution
# restored from the cache satisfy it, and that is the point: a cached run and a computed run go
# through *one* renderer, so they cannot produce two different reports.
ReportInput = SessionAnalysis | SessionContribution
