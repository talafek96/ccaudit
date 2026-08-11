"""Which pricing lane each resident item sat in, per turn — the cost-model bridge.

Every resident item is charged at one of three wildly different rates on any given turn
(docs/cost-model.md §5.2), and the lane matters more than the token count:

===========  ==========================================  ===================================
``cached``   kept loaded, served from cache               0.1x base input
``uncached`` **below the model's minimum cacheable
             prefix** — never caches, billed as fresh
             input                                        1x base input, *every turn*
``loading``  written into the cache this turn             1.25x (5m) or 2x (1h)
===========  ==========================================  ===================================

**Resident does not mean cached.** There is a minimum cacheable prefix; below it content
fails to cache silently — no error, ``cache_creation_input_tokens: 0`` — and is billed at
full price on every single turn. A measured 984-token ``CLAUDE.md`` caches on Opus 5 and does
not on Opus 4.6: the same file, ten times more expensive per turn. That is invariant **L1**,
and it is the whole reason this module exists:

    an item is ``uncached`` only when ``item.size_tokens < threshold(turn.model)``,

with the threshold looked up in the pricing table **for that turn's own model**. It is never
inferred from a version ordering — the thresholds are not monotonic across generations (Opus
4.7 needs 2048; the newer Opus 5 needs 512), so a corpus spanning models spans thresholds.

**Observe, don't predict** (docs/cost-model.md §5.1). The transcript records what was actually
charged. Lane mechanics *explain* those numbers; they never derive one. So where a turn's
records cannot support a classification — no cache read and no cache write to explain what
happened to a carried item — this module emits **no lane at all** rather than a plausible
guess, and the corresponding cost lands in the visible unattributed remainder (FR-019).

**A cache miss has four distinct causes** and they are never collapsed (FR-082): content left
the conversation (``evicted``), something earlier in the prefix changed (``invalidated``), it
was never eligible (``never_eligible``), or the breakpoint's 20-block lookback walked past it
(``lookback_miss``). Each implies a different fix, so merging them destroys the advice.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ccaudit.config import MissingThresholdError, Pricing
from ccaudit.model.residency import Timeline

LANES: tuple[str, ...] = ("cached", "uncached", "loading")
LANE_REASONS: tuple[str, ...] = ("cacheable", "below_minimum", "first_load", "reload_forced")

# Why content stopped being served from cache. Four causes, four different fixes — collapsing
# them into "cache miss" destroys the tool's advice (docs/cost-model.md §4, FR-082).
MISS_CAUSES: tuple[str, ...] = ("evicted", "invalidated", "never_eligible", "lookback_miss")

# A cache breakpoint walks back at most this many content blocks looking for a prior entry. A
# turn that adds more finds nothing and silently misses — routine in agentic loops with many
# tool_use/tool_result pairs. This is a structural property of the API, not a price, which is
# why it lives here (the one place cache-miss mechanics are decided) rather than in the
# pricing table.
MAX_LOOKBACK_BLOCKS = 20


@dataclass(frozen=True)
class LaneAssignment:
    """One (turn, item) verdict: which lane, why, and how much to trust it.

    ``threshold_tokens`` is the minimum cacheable prefix that was applied, carried so a reader
    can re-derive the verdict without rerunning the tool (FR-015). It is ``None`` only when
    the pricing table records no threshold for the model, which caps confidence rather than
    inventing one.
    """

    turn_index: int
    item_id: str
    model: str
    lane: str
    lane_reason: str
    size_tokens: int
    threshold_tokens: int | None
    confidence: str

    def __post_init__(self) -> None:
        if self.lane not in LANES:
            raise ValueError(
                f"turn {self.turn_index}, item {self.item_id!r}: unknown lane "
                f"{self.lane!r}; known: {LANES}"
            )
        if self.lane_reason not in LANE_REASONS:
            raise ValueError(
                f"turn {self.turn_index}, item {self.item_id!r}: unknown lane reason "
                f"{self.lane_reason!r}; known: {LANE_REASONS}"
            )
        # Invariant L1, enforced at the point of construction so no code path can produce an
        # `uncached` verdict without the threshold that justifies it.
        if self.lane == "uncached":
            if self.threshold_tokens is None:
                raise ValueError(
                    f"turn {self.turn_index}, item {self.item_id!r}: classified uncached with "
                    f"no threshold. An item is below the minimum only against a known "
                    f"threshold for that turn's model (invariant L1)"
                )
            if self.size_tokens >= self.threshold_tokens:
                raise ValueError(
                    f"turn {self.turn_index}, item {self.item_id!r}: classified uncached at "
                    f"{self.size_tokens} tokens against a minimum of {self.threshold_tokens} "
                    f"on {self.model!r} (invariant L1: uncached requires size < threshold)"
                )


@dataclass(frozen=True)
class CacheMiss:
    """One reason one item was not served from cache on one turn (FR-082).

    ``detail`` is the user-facing sentence: the four causes have four different fixes, so the
    cause is useless to a reader without it.
    """

    turn_index: int
    item_id: str
    cause: str
    detail: str
    confidence: str

    def __post_init__(self) -> None:
        if self.cause not in MISS_CAUSES:
            raise ValueError(
                f"turn {self.turn_index}, item {self.item_id!r}: unknown cache-miss cause "
                f"{self.cause!r}; known: {MISS_CAUSES}"
            )


@dataclass(frozen=True)
class ItemLaneSummary:
    """One item's lane history across the session — the per-file cache story.

    Counted in **token-turns** (``size_tokens`` x turns in the lane) as well as turns, because
    that is the weight an attribution split runs on; converting it to money is
    ``attribute.py``'s job, which owns the rates.
    """

    item_id: str
    size_tokens: int
    turns_by_lane: Mapping[str, int]
    token_turns_by_lane: Mapping[str, int]
    # Models this session ran on where this item was below the minimum cacheable prefix. A
    # first-class finding, not a footnote: it is a ~10x per-turn difference on the same file
    # and a reader must not have to go looking for it (FR-078).
    never_cacheable_on: tuple[str, ...]
    # Models whose threshold the pricing table does not record. The cacheability check could
    # not be run for these — stated, never assumed to have passed.
    threshold_unknown_on: tuple[str, ...]

    @property
    def full_rate_token_turns(self) -> int:
        """Token-turns charged at full input rate — the sub-threshold lane."""
        return self.token_turns_by_lane.get("uncached", 0)

    @property
    def reduced_rate_token_turns(self) -> int:
        """Token-turns charged at the 0.1x cache-read rate."""
        return self.token_turns_by_lane.get("cached", 0)

    @property
    def is_never_cacheable(self) -> bool:
        return bool(self.never_cacheable_on)


@dataclass
class LaneClassification:
    """Every lane verdict for a session, plus the misses and the gaps."""

    assignments: list[LaneAssignment] = field(default_factory=list)
    misses: list[CacheMiss] = field(default_factory=list)
    # Models the session used whose minimum cacheable prefix the pricing table does not
    # record. A recoverable gap: the rest of the breakdown still holds, so we say the check
    # could not be run rather than assuming the content cached (which is the direction that
    # hides the error).
    threshold_unknown_models: tuple[str, ...] = ()

    def at(self, turn_index: int) -> list[LaneAssignment]:
        """Every lane verdict for one turn, in timeline order so figures are reproducible."""
        return [a for a in self.assignments if a.turn_index == turn_index]

    def lane_weights(self, turn_index: int, lane: str) -> tuple[list[str], list[int]]:
        """Item ids and token weights for one lane on one turn — the input to a split.

        Mirrors :meth:`Timeline.weights_at` deliberately: a lane pool is divided among the
        items *in that lane*, never against one undifferentiated resident set (§5.2).
        """
        if lane not in LANES:
            raise ValueError(f"unknown lane {lane!r}; known: {LANES}")
        chosen = [a for a in self.assignments if a.turn_index == turn_index and a.lane == lane]
        return ([a.item_id for a in chosen], [a.size_tokens for a in chosen])

    def uncached_tokens_at(self, turn_index: int) -> int:
        """Tokens that could not cache this turn, and so were billed as fresh input.

        A ceiling on how much of a turn's observed ``input_tokens`` sub-threshold content can
        explain. The caller charges the *observed* figure, capped by this — never this figure
        in place of the observation.
        """
        return sum(
            a.size_tokens
            for a in self.assignments
            if a.turn_index == turn_index and a.lane == "uncached"
        )

    def summaries(self) -> list[ItemLaneSummary]:
        """Per-item lane histories, ordered by item id for a stable report."""
        by_item: dict[str, list[LaneAssignment]] = {}
        for assignment in self.assignments:
            by_item.setdefault(assignment.item_id, []).append(assignment)
        return [_summarize(item_id, group) for item_id, group in sorted(by_item.items())]

    def summary_for(self, item_id: str) -> ItemLaneSummary | None:
        """One item's lane history, or ``None`` when it was never resident on a priced turn."""
        group = [a for a in self.assignments if a.item_id == item_id]
        return _summarize(item_id, group) if group else None

    def misses_by_cause(self) -> dict[str, int]:
        """How many (turn, item) misses each cause explains. Never collapsed (FR-082)."""
        counts = dict.fromkeys(MISS_CAUSES, 0)
        for miss in self.misses:
            counts[miss.cause] += 1
        return counts


def classify_session(
    timeline: Timeline,
    pricing: Pricing,
    *,
    forced_reload_turns: Mapping[int, str] | None = None,
) -> LaneClassification:
    """Classify every (turn, resident item) into a lane, and name every cache miss.

    ``forced_reload_turns`` maps a turn index to the user-facing detail of the invalidation
    detected there (see :mod:`ccaudit.model.invalidation`). It is what turns a carried item's
    re-write from an unexplained charge into ``reload_forced`` with a named cause; without it
    such a turn produces no lane, which is the honest answer rather than a guess.

    Raises on a model absent from the pricing table (fail-fast, Principle I). A *missing
    threshold* for a known model is different — a recoverable gap in the rate table, recorded
    in ``threshold_unknown_models`` and confidence-capped.
    """
    forced = dict(forced_reload_turns or {})
    result = LaneClassification()
    unknown_thresholds: set[str] = set()
    thresholds: dict[str, int | None] = {}

    for turn_index, turn in enumerate(timeline.turns):
        threshold = _threshold_for(pricing, turn.model, thresholds, unknown_thresholds)
        result.assignments.extend(
            _classify_turn(timeline, turn_index, threshold, forced, result.misses)
        )
        _note_structural_misses(timeline, turn_index, forced, result.misses)

    result.threshold_unknown_models = tuple(sorted(unknown_thresholds))
    return result


def _threshold_for(
    pricing: Pricing,
    model: str,
    cache: dict[str, int | None],
    unknown: set[str],
) -> int | None:
    """The minimum cacheable prefix for a model, or ``None`` when the table does not say.

    Looked up per model, never derived: the thresholds are not monotonic across generations,
    so there is no ordering to infer one from (docs/cost-model.md §2, FR-079).
    """
    if model in cache:
        return cache[model]
    try:
        threshold: int | None = pricing.min_cacheable_tokens(model)
    except MissingThresholdError:
        threshold = None
        unknown.add(model)
    cache[model] = threshold
    return threshold


def _classify_turn(
    timeline: Timeline,
    turn_index: int,
    threshold: int | None,
    forced: Mapping[int, str],
    misses: list[CacheMiss],
) -> list[LaneAssignment]:
    turn = timeline.turns[turn_index]
    usage = turn.usage
    injected = {injection.item_id for injection in timeline.injections_at(turn_index)}

    # One verdict per item, even when two spans of the same item cover this turn (a file
    # evicted and re-read on the same boundary turn) — the lane is a property of the pair.
    resident: dict[str, int] = {}
    for span in timeline.resident_at(turn_index):
        item = timeline.items[span.item_id]
        resident.setdefault(item.item_id, item.size_tokens)

    below_minimum = {
        item_id: size
        for item_id, size in resident.items()
        if threshold is not None and size < threshold
    }
    # Observe, don't predict: sub-threshold content is billed as fresh input, so the turn's
    # observed `input_tokens` has to be big enough to contain it. When it is not, the records
    # contradict the mechanism (a shared prefix, a breakpoint we cannot see) and the verdict
    # is kept but its confidence dropped rather than asserted over the observation.
    demand = sum(below_minimum.values())
    below_confidence = "high" if demand and usage.input_tokens >= demand else "low"

    assignments: list[LaneAssignment] = []
    for item_id, size in resident.items():
        lane, reason, confidence = _lane_for(
            below_minimum=item_id in below_minimum,
            below_confidence=below_confidence,
            injected=item_id in injected,
            forced=turn_index in forced,
            usage_read=usage.cache_read_tokens,
            usage_write=usage.cache_creation_tokens,
            threshold_known=threshold is not None,
        )
        if lane is None:
            # No read and no write to explain what happened to a carried item, and no detected
            # cause. The records cannot support a lane, so none is asserted (FR-019).
            continue
        assignments.append(
            LaneAssignment(
                turn_index=turn_index,
                item_id=item_id,
                model=turn.model,
                lane=lane,
                lane_reason=reason,
                size_tokens=size,
                threshold_tokens=threshold,
                confidence=confidence,
            )
        )
        if lane == "uncached":
            misses.append(
                CacheMiss(
                    turn_index=turn_index,
                    item_id=item_id,
                    cause="never_eligible",
                    detail=(
                        f"{size} tokens is below the {threshold}-token minimum cacheable "
                        f"prefix on {turn.model} — billed at full rate every turn"
                    ),
                    confidence=confidence,
                )
            )
        elif reason == "reload_forced":
            misses.append(
                CacheMiss(
                    turn_index=turn_index,
                    item_id=item_id,
                    cause="invalidated",
                    detail=forced[turn_index],
                    confidence=confidence,
                )
            )
    return assignments


def _lane_for(
    *,
    below_minimum: bool,
    below_confidence: str,
    injected: bool,
    forced: bool,
    usage_read: int,
    usage_write: int,
    threshold_known: bool,
) -> tuple[str | None, str, str]:
    """The lane, its reason, and its confidence for one resident item on one turn.

    Order matters and encodes the cost model:

    1. **Below the minimum wins outright.** Sub-threshold content cannot enter the cache at
       all, so it is never "loading" and never "cached" — it is billed as fresh input on every
       turn including the one it arrived on (invariant L1, state transitions: ``uncached ->
       uncached``, never transitions).
    2. **Arriving or forced back in this turn is ``loading``** — the write lane, 1.25x or 2x —
       **but only if a write was actually charged.**
    3. **Otherwise cached**, but only where a cache read was actually observed.
    4. **Otherwise no verdict**, returned as ``None``.

    Step 2's condition is the "observe, don't predict" rule doing real work. A turn that
    charged a cache *read* and no cache *write* did not write anything, whatever we inferred
    about arrivals or prefix changes. Claiming the write lane there would exclude the item
    from the read pool it was actually being charged in, and its share of an observed charge
    would silently fall into the unattributed remainder. The transcript wins over the
    mechanism, every time (docs/cost-model.md §5.1).
    """
    if below_minimum:
        return "uncached", "below_minimum", below_confidence

    if (injected or forced) and usage_write:
        reason = "reload_forced" if forced else "first_load"
        # Carried content re-written because something earlier in the prefix changed is
        # `reload_forced`; its *cost* is charged to the change, not to this item (FR-081).
        return "loading", reason, _cap("high", threshold_known)

    if usage_read:
        # Read charged, nothing written: this content was served from cache this turn. That
        # includes content we believed had just arrived — a breakpoint placed elsewhere, or a
        # prefix change that did not reach it.
        confidence = "high" if not (injected or forced) else "low"
        return "cached", "cacheable", _cap(confidence, threshold_known)

    return None, "cacheable", "low"


def _cap(confidence: str, threshold_known: bool) -> str:
    """Drop confidence where the model's minimum cacheable prefix is not on record.

    Without the threshold we cannot rule out that the item was sub-threshold, which is a 10x
    per-turn difference. The verdict is still the best available; it is not a high-confidence
    one (docs/cost-model.md §5.5).
    """
    return confidence if threshold_known else "low"


def _note_structural_misses(
    timeline: Timeline,
    turn_index: int,
    forced: Mapping[int, str],
    misses: list[CacheMiss],
) -> None:
    """Record the two misses that are properties of the turn, not of an item's size.

    ``evicted`` is read straight off the residency span's own end reason — compaction records
    exactly what survived, so this needs no heuristic. ``lookback_miss`` is claimed only when
    the observed signature *and* the structural precondition are both present, because it is
    the one cause with no direct record behind it.
    """
    for span in timeline.spans:
        if span.end_reason == "evicted" and span.last_turn == turn_index:
            misses.append(
                CacheMiss(
                    turn_index=turn_index,
                    item_id=span.item_id,
                    cause="evicted",
                    detail="left the conversation at a compaction boundary",
                    confidence="high",
                )
            )

    if turn_index == 0 or turn_index in forced or turn_index in timeline.compaction_turns:
        return
    usage = timeline.turns[turn_index].usage
    if usage.cache_read_tokens:
        return
    blocks_added = len(timeline.injections_at(turn_index))
    if blocks_added <= MAX_LOOKBACK_BLOCKS:
        # Nothing observed says the lookback was the cause, so nothing is claimed. The cost of
        # this turn stays unattributed rather than being explained by a guess.
        return
    carried = [
        span.item_id for span in timeline.resident_at(turn_index) if span.first_turn < turn_index
    ]
    for item_id in dict.fromkeys(carried):
        misses.append(
            CacheMiss(
                turn_index=turn_index,
                item_id=item_id,
                cause="lookback_miss",
                detail=(
                    f"{blocks_added} content blocks were added, past the "
                    f"{MAX_LOOKBACK_BLOCKS}-block lookback a cache breakpoint walks — the "
                    f"prior entry could not be found"
                ),
                confidence="low",
            )
        )


def _summarize(item_id: str, group: Sequence[LaneAssignment]) -> ItemLaneSummary:
    turns_by_lane = dict.fromkeys(LANES, 0)
    token_turns_by_lane = dict.fromkeys(LANES, 0)
    never_cacheable: set[str] = set()
    unknown: set[str] = set()

    for assignment in group:
        turns_by_lane[assignment.lane] += 1
        token_turns_by_lane[assignment.lane] += assignment.size_tokens
        if assignment.lane == "uncached":
            never_cacheable.add(assignment.model)
        if assignment.threshold_tokens is None:
            unknown.add(assignment.model)

    return ItemLaneSummary(
        item_id=item_id,
        size_tokens=max(assignment.size_tokens for assignment in group),
        turns_by_lane=turns_by_lane,
        token_turns_by_lane=token_turns_by_lane,
        never_cacheable_on=tuple(sorted(never_cacheable)),
        threshold_unknown_on=tuple(sorted(unknown)),
    )
