"""Split each turn's observed charges across the items that caused them.

**Observe, don't predict.** The transcript records what was *actually* charged per turn —
``input_tokens``, ``cache_creation_input_tokens``, ``cache_read_input_tokens``,
``output_tokens``, and the model. Cache mechanics explain an observed number here; they never
derive one (docs/cost-model.md §5.1). That is what keeps the cache complications tractable
rather than fatal.

Each turn's four charges map onto four attribution components:

===================  ===============================================================
``cache_write``      **direct** — the forced-reload portion goes to the *change*
                     that caused it (FR-081); the rest to the items that newly
                     arrived this turn.
``cache_read``       **carry** — split by policy across the items in the *cached*
                     lane, not across the whole resident set.
``fresh_input``      **carry** for resident content below the model's minimum
                     cacheable prefix, which is re-sent at full rate every turn;
                     **overhead** for the remainder — the exchange itself.
``output``           **output** — charged to the exchange. Never to a file (FR-005,
                     invariant A2).
===================  ===============================================================

Each item is charged in exactly one lane per turn, so the same content is never billed twice
at two different rates (docs/cost-model.md §5.2).

**Reconciliation is at session level, never per turn.** Measured on real transcripts, the
ratio of a turn's ``cache_creation`` to the size of the tool result that preceded it has a
median of 3.31, and one 61,526-character read produced just 1,212 cache-creation tokens the
next turn — cache-breakpoint placement decouples the two. So a turn-level join is a
best-effort input to the session total, and whatever does not join lands visibly in the
unattributed remainder (docs/cost-model.md §5.3).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from ccaudit.config import Pricing
from ccaudit.ingest.records import AttachmentRecord, TurnRecord
from ccaudit.model.invalidation import (
    InvalidationEvent,
    TurnWrite,
    detect_invalidations,
    forced_reload_micros_at,
    reload_details,
)
from ccaudit.model.lanes import LaneClassification, classify_session
from ccaudit.model.policy import DEFAULT_POLICY, split_pool
from ccaudit.model.residency import Timeline
from ccaudit.money import cost_micros

TARGET_KINDS: tuple[str, ...] = ("item", "invalidation_event", "prompt", "unattributed")


@dataclass(frozen=True)
class Attribution:
    """One conclusion: this much cost, to this target, for this reason.

    ``basis``, ``confidence``, and ``source_refs`` are not optional metadata — they are what
    lets a skeptic check the number without rerunning the tool (FR-014, FR-015), and what
    stops an exact-looking figure from being read as more certain than it is.
    """

    session_id: str
    turn_index: int
    target_kind: str
    target_id: str | None
    component: str
    cost_micros: int
    basis: str
    confidence: str
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.target_kind not in TARGET_KINDS:
            raise ValueError(f"unknown target kind {self.target_kind!r}; known: {TARGET_KINDS}")
        if self.component == "output" and self.target_kind == "item":
            # Invariant A2. Generated output is caused by the exchange, not by any file that
            # happened to be resident while it was written.
            raise ValueError(
                f"output cost cannot target an item (got {self.target_id!r}); it belongs to "
                f"the exchange that produced it (FR-005)"
            )


@dataclass(frozen=True)
class TurnCharges:
    """What one turn was billed, priced from the observed token counts.

    Never adjusted. Attributions must reconcile back to these, not the other way round.
    """

    turn_index: int
    model: str
    fresh_input_micros: int
    cache_write_micros: int
    cache_read_micros: int
    output_micros: int
    ttl_confidence_cap: str | None

    @property
    def total_micros(self) -> int:
        return (
            self.fresh_input_micros
            + self.cache_write_micros
            + self.cache_read_micros
            + self.output_micros
        )


@dataclass
class AttributionResult:
    """Everything the pass concluded, ready to be reconciled and stored."""

    session_id: str
    policy: str
    attributions: list[Attribution] = field(default_factory=list)
    charges: list[TurnCharges] = field(default_factory=list)
    subagent_turns_rolled_up: int = 0
    # The prefix changes that forced content to reload, each carrying its own cost. First-class
    # rather than a note, because "what did adding that MCP server cost me?" is only answerable
    # if the reload is charged to the change instead of to the content it re-wrote (FR-081).
    invalidations: list[InvalidationEvent] = field(default_factory=list)
    # Which pricing lane each resident item sat in, per turn. Powers the per-item cached vs
    # full-rate split (SC-026) and the `never_cacheable_on` finding (FR-078).
    lanes: LaneClassification = field(default_factory=LaneClassification)

    @property
    def total_micros(self) -> int:
        return sum(charge.total_micros for charge in self.charges)


def price_turn(turn: TurnRecord, turn_index: int, pricing: Pricing) -> TurnCharges:
    """Price one turn's observed usage. Raises on an unknown model rather than guessing."""
    model = pricing.for_model(turn.model)
    usage = turn.usage
    # Each window priced at its own multiplier. A turn that writes into both is not a turn with
    # an unknown TTL — the record states how many tokens went to each — and treating it as one
    # priced the write at a blended guess *and* capped the whole session's precision at one
    # significant figure, which is how a $358.90 folder came to be displayed as "$400".
    unknown_multiplier, unknown_confidence = pricing.cache.write_multiplier(None)
    cache_write = (
        cost_micros(
            usage.cache_creation_5m_tokens, model.input_micros_per_mtok, pricing.cache.write_5m
        )
        + cost_micros(
            usage.cache_creation_1h_tokens, model.input_micros_per_mtok, pricing.cache.write_1h
        )
        + cost_micros(
            usage.cache_creation_unknown_tokens, model.input_micros_per_mtok, unknown_multiplier
        )
    )

    return TurnCharges(
        turn_index=turn_index,
        model=turn.model,
        fresh_input_micros=cost_micros(usage.input_tokens, model.input_micros_per_mtok),
        cache_write_micros=cache_write,
        cache_read_micros=cost_micros(
            usage.cache_read_tokens, model.input_micros_per_mtok, pricing.cache.read
        ),
        output_micros=cost_micros(usage.output_tokens, model.output_micros_per_mtok),
        # Capped only for the part whose window the record genuinely does not state. Capping a
        # turn whose windows *are* stated spends the report's precision on an uncertainty that
        # is not there.
        ttl_confidence_cap=(unknown_confidence if usage.cache_creation_unknown_tokens else None),
    )


def attribute_session(
    session_id: str,
    timeline: Timeline,
    pricing: Pricing,
    *,
    policy: str = DEFAULT_POLICY,
    attachments: Sequence[AttachmentRecord] = (),
) -> AttributionResult:
    """Attribute a whole session's charges to the items that caused them.

    Runs in three passes because each needs the one before it: price every turn, detect the
    prefix changes that forced content to reload, then classify every resident item into the
    pricing lane it was actually charged in. Only then is the split meaningful — attributing
    against one undifferentiated resident set would price sub-threshold content at a tenth of
    what it cost (§5.2).
    """
    result = AttributionResult(session_id=session_id, policy=policy)

    for turn_index, turn in enumerate(timeline.turns):
        if turn.is_sidechain:
            # Subagent work rolls up to the parent exchange and is counted exactly once
            # (FR-009). The charge is real and stays in the session total; what must not
            # happen is counting it at both the child and the parent level.
            result.subagent_turns_rolled_up += 1
        result.charges.append(price_turn(turn, turn_index, pricing))

    writes = [
        TurnWrite(
            turn_index=charge.turn_index,
            write_micros=charge.cache_write_micros,
            write_tokens=timeline.turns[charge.turn_index].usage.cache_creation_tokens,
            confidence_cap=charge.ttl_confidence_cap,
        )
        for charge in result.charges
    ]
    result.invalidations = detect_invalidations(
        timeline, attachments, writes, session_id=session_id
    )
    result.lanes = classify_session(
        timeline, pricing, forced_reload_turns=reload_details(result.invalidations)
    )

    for turn_index, turn in enumerate(timeline.turns):
        charges = result.charges[turn_index]
        source_ref = f"{turn.uuid}@line{turn.line}"
        result.attributions.extend(
            _attribute_turn(
                session_id,
                turn_index,
                charges,
                timeline,
                policy,
                source_ref,
                result.lanes,
                result.invalidations,
                pricing.for_model(turn.model).input_micros_per_mtok,
            )
        )

    return result


def _attribute_turn(
    session_id: str,
    turn_index: int,
    charges: TurnCharges,
    timeline: Timeline,
    policy: str,
    source_ref: str,
    lanes: LaneClassification,
    invalidations: Sequence[InvalidationEvent],
    input_rate_micros_per_mtok: int,
) -> list[Attribution]:
    attributions: list[Attribution] = []

    # -- output: the exchange, never a file (invariant A2) --------------------------------
    if charges.output_micros:
        attributions.append(
            Attribution(
                session_id=session_id,
                turn_index=turn_index,
                target_kind="prompt",
                target_id=None,
                component="output",
                cost_micros=charges.output_micros,
                basis="exact",
                confidence="high",
                source_refs=(source_ref,),
            )
        )

    # -- fresh input: sub-threshold content first, then conversation overhead --------------
    # The uncached remainder is the user's typing *plus* any resident content too small to
    # cache on this turn's model. That second part is a real per-turn charge against real
    # files, and it is the one a "resident ⇒ cheap" model misprices by a factor of ten.
    if charges.fresh_input_micros:
        attributions.extend(
            _attribute_uncached(
                session_id,
                turn_index,
                charges,
                lanes,
                source_ref,
                input_rate_micros_per_mtok,
            )
        )

    # -- cache write: the change that forced it, then what arrived -------------------------
    if charges.cache_write_micros:
        attributions.extend(
            _attribute_direct(session_id, turn_index, charges, timeline, source_ref, invalidations)
        )

    # -- cache read: carry cost, across the items actually in the cached lane ---------------
    if charges.cache_read_micros:
        attributions.extend(
            _attribute_carry(session_id, turn_index, charges, lanes, policy, source_ref)
        )

    return attributions


def _attribute_uncached(
    session_id: str,
    turn_index: int,
    charges: TurnCharges,
    lanes: LaneClassification,
    source_ref: str,
    input_rate_micros_per_mtok: int,
) -> list[Attribution]:
    """Charge sub-threshold resident content, then leave the rest as conversation overhead.

    Content below the model's minimum cacheable prefix never caches: it is re-sent as fresh
    input and billed at **full rate on every turn**, ten times the cached rate. That is a real
    recurring charge against a real file, and it is the cost a "resident ⇒ cheap" model gets
    wrong in the direction that hides the error.

    **The observation is the ceiling, never the substitute.** We charge the *observed*
    ``input_tokens`` figure, capped by what sub-threshold content could physically explain. If
    the turn's own fresh-input charge is smaller than that ceiling, the smaller number wins —
    we never bill a file for tokens the transcript does not show being charged.
    """
    item_ids, weights = lanes.lane_weights(turn_index, "uncached")
    if not item_ids:
        return _overhead_only(session_id, turn_index, charges.fresh_input_micros, source_ref)

    ceiling = cost_micros(lanes.uncached_tokens_at(turn_index), input_rate_micros_per_mtok)
    attributable = min(ceiling, charges.fresh_input_micros)
    if attributable <= 0:
        return _overhead_only(session_id, turn_index, charges.fresh_input_micros, source_ref)

    split = split_pool(attributable, weights, policy="proportional")
    attributions = [
        Attribution(
            session_id=session_id,
            turn_index=turn_index,
            target_kind="item",
            target_id=item_id,
            # Carry, not direct: this is the recurring per-turn charge for keeping the content
            # there, which is exactly what carry means. It simply happens at full rate.
            component="carry",
            cost_micros=share,
            basis="measured",
            confidence="medium",
            source_refs=(source_ref,),
        )
        for item_id, share in zip(item_ids, split.shares, strict=True)
        if share
    ]
    remainder = charges.fresh_input_micros - attributable
    attributions.extend(_overhead_only(session_id, turn_index, remainder, source_ref))
    return attributions


def _overhead_only(
    session_id: str, turn_index: int, micros: int, source_ref: str
) -> list[Attribution]:
    """The conversation itself: prompts, replies, and scaffolding. Never a file."""
    if micros <= 0:
        return []
    return [
        Attribution(
            session_id=session_id,
            turn_index=turn_index,
            target_kind="prompt",
            target_id=None,
            component="overhead",
            cost_micros=micros,
            basis="exact",
            confidence="high",
            source_refs=(source_ref,),
        )
    ]


def _attribute_direct(
    session_id: str,
    turn_index: int,
    charges: TurnCharges,
    timeline: Timeline,
    source_ref: str,
    invalidations: Sequence[InvalidationEvent],
) -> list[Attribution]:
    """Charge the forced reload to its cause, then the rest to what newly arrived.

    **Order matters.** A prefix-tier change — one MCP server added — re-writes the entire
    prompt including every instruction file. Splitting the whole write across arriving content
    would report "CLAUDE.md got more expensive" when the honest finding is "adding that server
    cost $X in forced re-writes" (FR-081). So the reload is taken off the top and charged to
    the change, and only the remainder is divided among the arrivals.

    That remainder is always split proportionally, regardless of the carry policy: a write is
    caused by the arriving content specifically, and its size is the only thing that could
    divide it. Anything the arrivals do not explain stays unattributed — never rounded onto a
    file.
    """
    attributions: list[Attribution] = []
    forced = forced_reload_micros_at(invalidations, turn_index)
    confidence = charges.ttl_confidence_cap or "medium"

    if forced:
        event = next(e for e in invalidations if e.turn_index == turn_index)
        attributions.append(
            Attribution(
                session_id=session_id,
                turn_index=turn_index,
                target_kind="invalidation_event",
                target_id=event.event_id,
                component="direct",
                cost_micros=forced,
                basis=event.basis,
                confidence=event.confidence,
                source_refs=(source_ref, *event.source_refs),
            )
        )

    remaining = charges.cache_write_micros - forced
    injections = timeline.injections_at(turn_index)
    if remaining <= 0 or not injections:
        return attributions

    weights = [injection.size_tokens for injection in injections]
    split = split_pool(remaining, weights, policy="proportional")
    attributions.extend(
        Attribution(
            session_id=session_id,
            turn_index=turn_index,
            target_kind="item",
            target_id=injection.item_id,
            component="direct",
            cost_micros=share,
            basis="measured",
            confidence=confidence,
            source_refs=(source_ref, injection.source_ref),
        )
        for injection, share in zip(injections, split.shares, strict=True)
        if share
    )
    return attributions


def _attribute_carry(
    session_id: str,
    turn_index: int,
    charges: TurnCharges,
    lanes: LaneClassification,
    policy: str,
    source_ref: str,
) -> list[Attribution]:
    """Split the re-show charge across the items actually in the cached lane this turn.

    This is the recurring half of the bill and the reason the tool exists. The figure rests
    on the splitting policy, so its confidence says so — a carry number is not a measurement
    of what one file cost, it is one defensible division of a shared charge.

    **The cached lane, not the whole resident set** (§5.2). Content being *written* this turn
    is already paying the write rate, and content below the model's minimum is paying full
    rate as fresh input; charging either of them a share of the read pool as well would bill
    the same content twice at two different rates. Anything resident but in no lane leaves its
    share unattributed, which is the honest answer when we cannot say what it was charged.
    """
    item_ids, weights = lanes.lane_weights(turn_index, "cached")
    if not item_ids:
        return []

    split = split_pool(charges.cache_read_micros, weights, policy=policy)
    return [
        Attribution(
            session_id=session_id,
            turn_index=turn_index,
            target_kind="item",
            target_id=item_id,
            component="carry",
            cost_micros=share,
            basis="measured",
            confidence="medium",
            source_refs=(source_ref,),
        )
        for item_id, share in zip(item_ids, split.shares, strict=True)
        if share
    ]
