"""Split each turn's observed charges across the items that caused them.

**Observe, don't predict.** The transcript records what was *actually* charged per turn —
``input_tokens``, ``cache_creation_input_tokens``, ``cache_read_input_tokens``,
``output_tokens``, and the model. Cache mechanics explain an observed number here; they never
derive one (docs/cost-model.md §5.1). That is what keeps the cache complications tractable
rather than fatal.

Each turn's four charges map onto four attribution components:

===================  ===============================================================
``cache_write``      **direct** — charged to the items newly injected this turn.
``cache_read``       **carry** — split across everything resident, by policy.
``fresh_input``      **overhead** — the exchange itself: prompts, replies, and
                     scaffolding. Never charged to a file in v1; the sub-threshold
                     portion becomes attributable once cache lanes land (US4).
``output``           **output** — charged to the exchange. Never to a file (FR-005,
                     invariant A2).
===================  ===============================================================

**Reconciliation is at session level, never per turn.** Measured on real transcripts, the
ratio of a turn's ``cache_creation`` to the size of the tool result that preceded it has a
median of 3.31, and one 61,526-character read produced just 1,212 cache-creation tokens the
next turn — cache-breakpoint placement decouples the two. So a turn-level join is a
best-effort input to the session total, and whatever does not join lands visibly in the
unattributed remainder (docs/cost-model.md §5.3).
"""

from dataclasses import dataclass, field

from ccaudit.config import Pricing
from ccaudit.ingest.records import TurnRecord
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

    @property
    def total_micros(self) -> int:
        return sum(charge.total_micros for charge in self.charges)


def price_turn(turn: TurnRecord, turn_index: int, pricing: Pricing) -> TurnCharges:
    """Price one turn's observed usage. Raises on an unknown model rather than guessing."""
    model = pricing.for_model(turn.model)
    usage = turn.usage
    write_multiplier, confidence_cap = pricing.cache.write_multiplier(usage.ttl)

    return TurnCharges(
        turn_index=turn_index,
        model=turn.model,
        fresh_input_micros=cost_micros(usage.input_tokens, model.input_micros_per_mtok),
        cache_write_micros=cost_micros(
            usage.cache_creation_tokens, model.input_micros_per_mtok, write_multiplier
        ),
        cache_read_micros=cost_micros(
            usage.cache_read_tokens, model.input_micros_per_mtok, pricing.cache.read
        ),
        output_micros=cost_micros(usage.output_tokens, model.output_micros_per_mtok),
        # A write whose reuse window the record does not state is priced at 5m and capped in
        # confidence rather than assumed — assuming 5m where 1h applied understates it by 60%.
        ttl_confidence_cap=confidence_cap if usage.cache_creation_tokens else None,
    )


def attribute_session(
    session_id: str,
    timeline: Timeline,
    pricing: Pricing,
    *,
    policy: str = DEFAULT_POLICY,
) -> AttributionResult:
    """Attribute a whole session's charges to the items that caused them."""
    result = AttributionResult(session_id=session_id, policy=policy)

    for turn_index, turn in enumerate(timeline.turns):
        if turn.is_sidechain:
            # Subagent work rolls up to the parent exchange and is counted exactly once
            # (FR-009). The charge is real and stays in the session total; what must not
            # happen is counting it at both the child and the parent level.
            result.subagent_turns_rolled_up += 1

        charges = price_turn(turn, turn_index, pricing)
        result.charges.append(charges)
        source_ref = f"{turn.uuid}@line{turn.line}"

        result.attributions.extend(
            _attribute_turn(session_id, turn_index, charges, timeline, policy, source_ref)
        )

    return result


def _attribute_turn(
    session_id: str,
    turn_index: int,
    charges: TurnCharges,
    timeline: Timeline,
    policy: str,
    source_ref: str,
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

    # -- fresh input: conversation overhead ------------------------------------------------
    # The uncached remainder is the user's typing plus any resident content too small to
    # cache. Separating the second part needs per-turn cache lanes (US4); until then it is
    # reported as overhead rather than guessed at, so no file is charged for it wrongly.
    if charges.fresh_input_micros:
        attributions.append(
            Attribution(
                session_id=session_id,
                turn_index=turn_index,
                target_kind="prompt",
                target_id=None,
                component="overhead",
                cost_micros=charges.fresh_input_micros,
                basis="exact",
                confidence="high",
                source_refs=(source_ref,),
            )
        )

    # -- cache write: direct cost, to what arrived this turn -------------------------------
    if charges.cache_write_micros:
        attributions.extend(
            _attribute_direct(session_id, turn_index, charges, timeline, policy, source_ref)
        )

    # -- cache read: carry cost, across the resident set -----------------------------------
    if charges.cache_read_micros:
        attributions.extend(
            _attribute_carry(session_id, turn_index, charges, timeline, policy, source_ref)
        )

    return attributions


def _attribute_direct(
    session_id: str,
    turn_index: int,
    charges: TurnCharges,
    timeline: Timeline,
    policy: str,
    source_ref: str,
) -> list[Attribution]:
    """Charge the write to what newly arrived, in proportion to how much each brought.

    Always proportional, regardless of the carry policy: a write is caused by the arriving
    content specifically, and its size is the only thing that could divide it. Anything the
    arrivals do not explain is left unattributed — never rounded up onto a file.
    """
    injections = timeline.injections_at(turn_index)
    if not injections:
        return []

    weights = [injection.size_tokens for injection in injections]
    split = split_pool(charges.cache_write_micros, weights, policy="proportional")

    # Confidence is capped where the reuse window is unknown, and the basis says the figure
    # rests on a turn-level join that the cost model warns is loose (§5.3).
    confidence = charges.ttl_confidence_cap or "medium"
    return [
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
    ]


def _attribute_carry(
    session_id: str,
    turn_index: int,
    charges: TurnCharges,
    timeline: Timeline,
    policy: str,
    source_ref: str,
) -> list[Attribution]:
    """Split the re-show charge across everything resident this turn.

    This is the recurring half of the bill and the reason the tool exists. The figure rests
    on the splitting policy, so its confidence says so — a carry number is not a measurement
    of what one file cost, it is one defensible division of a shared charge.
    """
    item_ids, weights = timeline.weights_at(turn_index)
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
