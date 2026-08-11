"""The report-data payload — one contract, three consumers.

``--json``, the self-contained HTML report, and the interactive UI all render *this* shape and
nothing else (``specs/001-per-file-cost-attribution/contracts/report-data.md``). That is what
makes FR-074 — every figure available anywhere is obtainable from the terminal — structurally
true rather than a promise: a surface cannot show a figure this payload does not carry.

Three rules govern everything here:

**It adds up or it is not produced.** ``attributed + unattributed == cost_micros``, exact
integer equality, asserted before the payload is returned. A consumer must never have to
decide what to do with a breakdown that contradicts its own total (Principle X, SC-001).

**Labels come from the registry, never from a renderer.** Plain-language names, significant
figures, and policy descriptions are read from :mod:`ccaudit.config.components` and
:mod:`ccaudit.model.policy`. A label re-typed here would be a second source of truth
(Principle IX, FR-016).

**Deterministic to the byte.** Same analyses in, same payload out, on every machine (FR-017,
SC-009). Every list is sorted explicitly; nothing is emitted in set or dict-insertion order.
The one exception is ``generated_at``, which is a clock reading and can be pinned by the
caller.

Sections deferred to later milestones — ``tree``, ``turns``, ``residency``,
``invalidations``, ``comparison`` — are emitted as empty containers rather than invented. An
absent section is honest; a fabricated one is not.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from ccaudit import __version__
from ccaudit.analyse import SessionAnalysis
from ccaudit.config import (
    ATTRIBUTION_COMPONENTS,
    CHARGE_COMPONENTS,
    MissingThresholdError,
    Pricing,
    sig_figs_for,
)
from ccaudit.config.components import BASIS_VALUES, CONFIDENCE_VALUES
from ccaudit.model.policy import describe as describe_policy
from ccaudit.model.reconcile import ReconciliationError

SCHEMA_VERSION = "1.0"

# The only value this field may ever take. Named here so that a grep for "billed" finds
# nothing to change: the figures are imputed from list prices and are not amounts charged.
COST_BASIS = "api_equivalent_estimate"
CURRENCY = "USD"

# Totals and per-component figures are sums of token counts recorded in the transcript, so
# they are as exact as the rates allow; the uncertainty left in them is a systematic price
# error, which the cost-basis label and the paired share already carry (components.py).
TOTALS_CONFIDENCE = "high"

PSEUDONYM_PREFIX = "redacted-"
PSEUDONYM_HEX_DIGITS = 8


@dataclass
class _ItemRollup:
    """One context item's figures, accumulated across every session it appears in."""

    item_id: str
    kind: str
    identity: str
    category: str
    size_tokens: int
    direct_micros: int = 0
    carry_micros: int = 0
    reads: int = 0
    turns_resident: int = 0
    basis: str = BASIS_VALUES[0]
    confidence: str = CONFIDENCE_VALUES[0]
    per_session: dict[str, int] = field(default_factory=dict)
    never_cacheable_on: set[str] = field(default_factory=set)

    @property
    def total_micros(self) -> int:
        return self.direct_micros + self.carry_micros


# Grouping dimensions (FR-007). `item` is the ungrouped view; the rest merge rows.
#
# `folder` groups by an item's **immediate parent directory**, not by every ancestor. Rolling
# a file up into all of its ancestors at once would count it several times in one flat table,
# and a table that double-counts is the same defect as one that drops a row. The
# every-level-of-the-hierarchy view (FR-034) is the `tree` section, where a node can carry both
# its own cost and its subtree's without the two being summed together.
GROUPINGS: tuple[str, ...] = ("item", "file", "folder", "ext", "category")
DEFAULT_GROUPING = "item"

_MIXED = "(mixed)"


class UnknownGroupingError(ValueError):
    """An unsupported `--by` dimension. Never silently falls back to the default."""


def build_report_data(
    analyses: Sequence[SessionAnalysis],
    *,
    redact: bool = False,
    sessions_excluded_count: int = 0,
    generated_at: str | None = None,
    group_by: str = DEFAULT_GROUPING,
) -> dict[str, Any]:
    """Build the report-data payload for one or more analysed sessions.

    ``group_by`` merges the item rows along one dimension (FR-007). Grouping only ever *merges*
    rows, so every dimension sums to the same attributed total — a grouping that changed the
    total would mean it had dropped or duplicated something.

    Raises :class:`~ccaudit.model.reconcile.ReconciliationError` if the payload would not add
    up, and ``ValueError`` for an empty selection or a mix of carry-splitting policies —
    figures produced under different policies are not comparable and must not be summed.
    """
    if group_by not in GROUPINGS:
        raise UnknownGroupingError(f"unknown grouping {group_by!r}; known: {list(GROUPINGS)}")
    if not analyses:
        raise ValueError(
            "cannot build report data from zero analyses; an empty selection is the caller's "
            "to report (exit code 2), not something to render as a zero-cost session"
        )
    policies = sorted({analysis.policy for analysis in analyses})
    if len(policies) > 1:
        raise ValueError(
            f"cannot combine sessions analysed under different carry-splitting policies "
            f"{policies}: per-item figures rest on the policy, so summing them would produce a "
            f"breakdown no single policy explains"
        )
    policy = policies[0]

    totals = _totals(analyses)
    rollups, threshold_gaps = _rollups(analyses)
    limitations = _limitations(analyses, threshold_gaps, rollups)

    grouped = _regroup(rollups, group_by)
    _assert_grouping_conserves(rollups, grouped, group_by)
    items = [_item_payload(rollup, totals["cost_micros"], redact=redact) for rollup in grouped]

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "tool_version": __version__,
        "cost_basis": COST_BASIS,
        "currency": CURRENCY,
        "policy": policy,
        "group_by": group_by,
        "redacted": redact,
        "scope": _scope(analyses, sessions_excluded_count),
        "totals": {**totals, "uncertainty_notes": _uncertainty_notes(analyses, policy)},
        "components": _components(analyses, totals["cost_micros"]),
        # How the charges were *concluded* to divide up, as opposed to how they were incurred.
        # It carries the two conclusions that are never charged to a file — the conversation
        # itself and what the model wrote back — without which a per-item table plus the
        # remainder does not reach the total, and a reader is left with a silent gap.
        "attribution": _attribution(analyses, totals["cost_micros"]),
        "items": items,
        # Deferred to later milestones (US4-US6). Emitted empty so a consumer can branch on
        # "not yet computed" rather than on a missing key, and so nothing is invented here.
        "tree": {},
        "turns": [],
        "residency": [],
        "invalidations": [],
        "comparison": {},
        "diagnostics": {
            "unparseable_records": sum(a.parsed.unparseable_count for a in analyses),
            # Anchor reconciliation is parsed (ingest/anchors.py) but not yet wired into
            # SessionAnalysis; an empty list says "not checked", never "checked and clean".
            "anchor_reconciliation": [],
            "limitations": limitations,
            "estimated_figures": sum(
                1
                for item in items
                if item["basis"] == BASIS_VALUES[-1]  # "estimated"
            ),
        },
    }
    _assert_adds_up(payload)
    return payload


def _assert_adds_up(payload: dict[str, Any]) -> None:
    """Refuse to hand back a payload whose parts contradict its total (Principle X, SC-001).

    Checked here as well as in the model layer because this is the last point before three
    independent consumers render the numbers. Integer equality, no epsilon — the moment a
    tolerance exists it becomes where a real misattribution hides.
    """
    totals = payload["totals"]
    if totals["attributed_micros"] + totals["unattributed_micros"] != totals["cost_micros"]:
        raise ReconciliationError(
            f"report payload does not add up: {totals['attributed_micros']} attributed + "
            f"{totals['unattributed_micros']} unattributed != {totals['cost_micros']} total"
        )
    items_total = sum(item["total_micros"] for item in payload["items"])
    if items_total > totals["attributed_micros"]:
        raise ReconciliationError(
            f"report payload over-attributes: per-item figures sum to {items_total} against "
            f"{totals['attributed_micros']} attributed. A charge was counted under more than "
            f"one item."
        )
    # The strong form of the invariant, and the one a reader can check by adding up a printed
    # table: every conclusion, plus the remainder, equals what the session cost.
    concluded = sum(component["cost_micros"] for component in payload["attribution"])
    if concluded + totals["unattributed_micros"] != totals["cost_micros"]:
        raise ReconciliationError(
            f"report payload does not add up by attribution component: {concluded} concluded "
            f"+ {totals['unattributed_micros']} unattributed != {totals['cost_micros']} total"
        )
    item_direct = sum(item["direct_micros"] for item in payload["items"])
    item_carry = sum(item["carry_micros"] for item in payload["items"])
    concluded_by_id = {c["id"]: c["cost_micros"] for c in payload["attribution"]}
    if item_direct != concluded_by_id["direct"] or item_carry != concluded_by_id["carry"]:
        raise ReconciliationError(
            f"per-item figures do not partition the item-level conclusions: direct "
            f"{item_direct} vs {concluded_by_id['direct']}, carry {item_carry} vs "
            f"{concluded_by_id['carry']}"
        )


def _totals(analyses: Sequence[SessionAnalysis]) -> dict[str, Any]:
    cost = sum(a.reconciliation.total_micros for a in analyses)
    attributed = sum(a.reconciliation.attributed_micros for a in analyses)
    unattributed = sum(a.reconciliation.unattributed_micros for a in analyses)

    tokens = {"fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    for analysis in analyses:
        for turn in analysis.timeline.turns:
            usage = turn.usage
            tokens["fresh_input"] += usage.input_tokens
            tokens["cache_write"] += usage.cache_creation_tokens
            tokens["cache_read"] += usage.cache_read_tokens
            tokens["output"] += usage.output_tokens

    return {
        "cost_micros": cost,
        "attributed_micros": attributed,
        "unattributed_micros": unattributed,
        "attributed_share": _share(attributed, cost),
        # Always present, whatever its size. A remainder that looks bad is still the finding
        # (FR-012, FR-013).
        "unattributed_share": _share(unattributed, cost),
        "tokens": tokens,
        "confidence": TOTALS_CONFIDENCE,
        "display_sig_figs": sig_figs_for(TOTALS_CONFIDENCE),
    }


def _scope(analyses: Sequence[SessionAnalysis], excluded: int) -> dict[str, Any]:
    versions: set[str] = set()
    for analysis in analyses:
        versions |= analysis.parsed.producing_versions
    return {
        "sessions_included": sorted(a.session_id for a in analyses),
        # Exclusion is part of the result, never a hidden input (FR-063).
        "sessions_excluded_count": excluded,
        "covered_through_turn": sum(len(a.timeline.turns) for a in analyses),
        "provisional": any(a.provisional for a in analyses),
        "producing_versions": sorted(versions),
    }


def _components(analyses: Sequence[SessionAnalysis], total_micros: int) -> list[dict[str, Any]]:
    """The four charge components, priced and labelled from the registry that defines them."""
    tokens = {component.id: 0 for component in CHARGE_COMPONENTS}
    micros = {component.id: 0 for component in CHARGE_COMPONENTS}

    for analysis in analyses:
        for turn in analysis.timeline.turns:
            usage = turn.usage
            tokens["fresh_input"] += usage.input_tokens
            tokens["cache_write"] += usage.cache_creation_tokens
            tokens["cache_read"] += usage.cache_read_tokens
            tokens["output"] += usage.output_tokens
        for charge in analysis.attribution.charges:
            micros["fresh_input"] += charge.fresh_input_micros
            micros["cache_write"] += charge.cache_write_micros
            micros["cache_read"] += charge.cache_read_micros
            micros["output"] += charge.output_micros

    return [
        {
            "id": component.id,
            "technical_name": component.technical_name,
            "plain_name": component.plain_name,
            "description": component.description,
            "tokens": tokens[component.id],
            "cost_micros": micros[component.id],
            "share": _share(micros[component.id], total_micros),
            "confidence": TOTALS_CONFIDENCE,
            "display_sig_figs": sig_figs_for(TOTALS_CONFIDENCE),
        }
        for component in CHARGE_COMPONENTS
    ]


def _attribution(analyses: Sequence[SessionAnalysis], total_micros: int) -> list[dict[str, Any]]:
    """The four attribution components — what the charges were concluded to be *for*.

    ``direct`` and ``carry`` are the item-level conclusions the leaderboard breaks down;
    ``overhead`` and ``output`` belong to the exchange and are never charged to a file
    (invariant A2, FR-005). All four plus the remainder equal the session total, which is what
    lets a printed table be added up by hand.
    """
    micros = {component.id: 0 for component in ATTRIBUTION_COMPONENTS}
    confidence = {component.id: CONFIDENCE_VALUES[0] for component in ATTRIBUTION_COMPONENTS}

    for analysis in analyses:
        for attribution in analysis.attribution.attributions:
            micros[attribution.component] += attribution.cost_micros
            confidence[attribution.component] = _weakest(
                confidence[attribution.component], attribution.confidence, CONFIDENCE_VALUES
            )

    return [
        {
            "id": component.id,
            "technical_name": component.technical_name,
            "plain_name": component.plain_name,
            "description": component.description,
            # Whether this conclusion can be broken down to an individual file at all.
            "per_item": component.id in ("direct", "carry"),
            "cost_micros": micros[component.id],
            "share": _share(micros[component.id], total_micros),
            "confidence": confidence[component.id],
            "display_sig_figs": sig_figs_for(confidence[component.id]),
        }
        for component in ATTRIBUTION_COMPONENTS
    ]


def _rollups(analyses: Sequence[SessionAnalysis]) -> tuple[list[_ItemRollup], list[str]]:
    """Accumulate per-item figures across sessions, ranked most expensive first.

    Returns the rollups and the models whose cacheability threshold could not be resolved —
    named in the limitations rather than silently treated as cacheable.
    """
    rollups: dict[str, _ItemRollup] = {}
    threshold_gaps: set[str] = set()
    # Read and residency counts come from the timeline, not from the attributions, so they are
    # folded in once per (session, item) rather than once per attribution row.
    counted: set[tuple[str, str]] = set()

    for analysis in analyses:
        timeline = analysis.timeline
        for attribution in analysis.attribution.attributions:
            if attribution.target_kind != "item" or attribution.target_id is None:
                continue
            item = timeline.items.get(attribution.target_id)
            if item is None:
                raise ReconciliationError(
                    f"session {analysis.session_id}: attribution targets item "
                    f"{attribution.target_id!r}, which the timeline does not contain. The "
                    f"breakdown cannot be described without it."
                )
            rollup = rollups.get(item.item_id)
            if rollup is None:
                rollup = _ItemRollup(
                    item_id=item.item_id,
                    kind=item.kind,
                    identity=item.identity,
                    category=item.category,
                    size_tokens=item.size_tokens,
                    basis=item.basis,
                    confidence=item.confidence,
                )
                rollups[item.item_id] = rollup
            else:
                # The same file in two sessions: the larger observed size is the one carried.
                rollup.size_tokens = max(rollup.size_tokens, item.size_tokens)

            key = (analysis.session_id, item.item_id)
            if key not in counted:
                counted.add(key)
                rollup.reads += timeline.load_count(item.item_id)
                rollup.turns_resident += timeline.turns_resident(item.item_id)

            if attribution.component == "direct":
                rollup.direct_micros += attribution.cost_micros
            else:
                rollup.carry_micros += attribution.cost_micros
            rollup.per_session[analysis.session_id] = (
                rollup.per_session.get(analysis.session_id, 0) + attribution.cost_micros
            )
            # A figure is only as trustworthy as its weakest input: the size measurement and
            # every attribution that fed it. Taking the weakest is what stops an estimated
            # size from being displayed at measured precision (FR-014, FR-095).
            rollup.basis = _weakest(rollup.basis, attribution.basis, BASIS_VALUES)
            rollup.confidence = _weakest(
                rollup.confidence, attribution.confidence, CONFIDENCE_VALUES
            )

    _mark_never_cacheable(analyses, rollups, threshold_gaps)
    return (
        sorted(rollups.values(), key=lambda r: (-r.total_micros, r.item_id)),
        sorted(threshold_gaps),
    )


def _regroup(rollups: list[_ItemRollup], group_by: str) -> list[_ItemRollup]:
    """Merge item rows along one dimension. Merging only — nothing is created or dropped."""
    if group_by == DEFAULT_GROUPING:
        return rollups

    merged: dict[str, _ItemRollup] = {}
    for rollup in rollups:
        key = _group_key(rollup, group_by)
        target = merged.get(key)
        if target is None:
            merged[key] = _ItemRollup(
                item_id=f"{group_by}:{key}",
                kind=rollup.kind,
                identity=key,
                category=rollup.category,
                size_tokens=rollup.size_tokens,
                direct_micros=rollup.direct_micros,
                carry_micros=rollup.carry_micros,
                reads=rollup.reads,
                turns_resident=rollup.turns_resident,
                basis=rollup.basis,
                confidence=rollup.confidence,
                per_session=dict(rollup.per_session),
                never_cacheable_on=set(rollup.never_cacheable_on),
            )
            continue

        target.direct_micros += rollup.direct_micros
        target.carry_micros += rollup.carry_micros
        target.reads += rollup.reads
        target.turns_resident += rollup.turns_resident
        target.size_tokens += rollup.size_tokens
        target.never_cacheable_on |= rollup.never_cacheable_on
        for session_id, cost in rollup.per_session.items():
            target.per_session[session_id] = target.per_session.get(session_id, 0) + cost
        # A merged row is only as trustworthy as its weakest member, and only as specific: two
        # categories in one bucket is "(mixed)", never whichever happened to arrive first.
        target.basis = _weakest(target.basis, rollup.basis, BASIS_VALUES)
        target.confidence = _weakest(target.confidence, rollup.confidence, CONFIDENCE_VALUES)
        if target.category != rollup.category:
            target.category = _MIXED
        if target.kind != rollup.kind:
            target.kind = _MIXED

    return sorted(merged.values(), key=lambda r: (-r.total_micros, r.item_id))


def _group_key(rollup: _ItemRollup, group_by: str) -> str:
    if group_by == "category":
        return rollup.category
    if group_by == "file":
        return rollup.identity
    if group_by == "folder":
        parent = PurePosixPath(rollup.identity).parent
        # An item with no directory part (a skill listing, a tool schema) is not in a folder;
        # saying so is more useful than filing it under ".".
        return str(parent) if str(parent) not in (".", "/") else f"({rollup.kind})"
    # extension
    suffix = PurePosixPath(rollup.identity).suffix
    return suffix or f"({rollup.kind})"


def _assert_grouping_conserves(
    original: list[_ItemRollup], grouped: list[_ItemRollup], group_by: str
) -> None:
    """A grouping must partition the rows exactly — none dropped, none counted twice.

    This is the same promise as the session-level reconciliation, one level down. A per-folder
    view that quietly loses a file looks entirely plausible in a report, which is precisely why
    it is checked rather than assumed (FR-007, FR-012).
    """
    before = sum(rollup.total_micros for rollup in original)
    after = sum(rollup.total_micros for rollup in grouped)
    if before != after:
        raise ReconciliationError(
            f"grouping by {group_by!r} changed the attributed total from {before} to {after} "
            f"(difference {after - before}). A grouping may only merge rows."
        )


def _mark_never_cacheable(
    analyses: Sequence[SessionAnalysis],
    rollups: dict[str, _ItemRollup],
    threshold_gaps: set[str],
) -> None:
    """Flag items too small to cache on a model the session used (FR-078).

    First-class, not a footnote: the same file moves from the 0.1x lane to the 1x lane across
    models, a ~10x per-turn difference, and a reader must not have to go looking for it
    (docs/cost-model.md §2).
    """
    for analysis in analyses:
        models = sorted({charge.model for charge in analysis.attribution.charges})
        for model in models:
            threshold = _min_cacheable_tokens(analysis.pricing, model, threshold_gaps)
            if threshold is None:
                continue
            # Only items this session actually carried: a model is evidence about the items
            # that were resident alongside it, not about every item in the corpus.
            for item_id in analysis.timeline.items:
                rollup = rollups.get(item_id)
                if rollup is not None and rollup.size_tokens < threshold:
                    rollup.never_cacheable_on.add(model)


def _min_cacheable_tokens(pricing: Pricing, model: str, gaps: set[str]) -> int | None:
    """The model's minimum cacheable prefix, or ``None`` when the table does not record it.

    A missing threshold is a genuine, recoverable gap in the rate table, not a broken
    invariant: the rest of the breakdown is still correct, so the answer is to say the
    cacheability check could not be run for this model rather than to refuse the whole report
    or — far worse — to assume the content cached.
    """
    try:
        return pricing.min_cacheable_tokens(model)
    except MissingThresholdError:
        gaps.add(model)
        return None


def _item_payload(rollup: _ItemRollup, total_micros: int, *, redact: bool) -> dict[str, Any]:
    driver = _uncertainty_driver(rollup)
    display = _display_name(rollup, redact=redact)
    payload: dict[str, Any] = {
        # The item id embeds the path, so under redaction it becomes the pseudonym too.
        # Blanking `display` and `identity` while leaving the id intact would have published
        # every path in the very field a consumer keys on — the exact leak FR-043 exists to
        # prevent, and invisible in a rendered report.
        "item_id": f"{rollup.kind}:{display}" if redact else rollup.item_id,
        "kind": rollup.kind,
        "display": display,
        "category": rollup.category,
        "size_tokens": rollup.size_tokens,
        "direct_micros": rollup.direct_micros,
        "carry_micros": rollup.carry_micros,
        "total_micros": rollup.total_micros,
        "share": _share(rollup.total_micros, total_micros),
        "reads": rollup.reads,
        "turns_resident": rollup.turns_resident,
        # Direct cost is the cache-write lane and carry cost is the cache-read lane, by
        # construction of the attribution pass. The sub-threshold lane needs per-turn lane
        # classification (US4); until it lands that cost sits in overhead, and is reported as
        # zero here rather than guessed at.
        "lanes": {
            "cached_micros": rollup.carry_micros,
            "uncached_micros": 0,
            "loading_micros": rollup.direct_micros,
        },
        "never_cacheable_on": sorted(rollup.never_cacheable_on),
        "basis": rollup.basis,
        "confidence": rollup.confidence,
        "display_sig_figs": sig_figs_for(rollup.confidence),
        "uncertainty": {
            **_uncertainty_range(rollup, driver),
            "driver": driver,
        },
        "per_session": [
            {"session_id": session_id, "total_micros": rollup.per_session[session_id]}
            for session_id in sorted(rollup.per_session)
        ],
    }
    if not redact:
        payload["identity"] = rollup.identity
    return payload


def _uncertainty_driver(rollup: _ItemRollup) -> str:
    """What dominates this figure's range (FR-096).

    Precedence is deliberate. An estimated size scales the whole figure, so it outranks
    everything. Otherwise, whichever of the two components is larger names the argument a
    disputant would actually make: carry rests on the splitting policy, direct rests on a
    turn-level join the cost model warns is loose (§5.3).
    """
    if rollup.basis == BASIS_VALUES[-1]:  # "estimated"
        return "size_estimate"
    if rollup.carry_micros > rollup.direct_micros:
        return "carry_split_policy"
    return "turn_level_join"


def _uncertainty_range(rollup: _ItemRollup, driver: str) -> dict[str, int]:
    """The band the driver could move this figure through.

    The width is the part of the figure the driver controls: the whole of it for an estimated
    size, the shared carry for a policy choice, the direct join otherwise. This is a *lower
    bound* on the true range — under the exclusive policy an item that was alone in context
    can be charged more than its proportional share — and it is deliberately not presented as
    a confidence interval, which would imply a distribution nobody measured.
    """
    total = rollup.total_micros
    if driver == "size_estimate":
        width = total
    elif driver == "carry_split_policy":
        width = rollup.carry_micros
    else:
        width = rollup.direct_micros
    return {"low_micros": max(0, total - width), "high_micros": total + width}


def _display_name(rollup: _ItemRollup, *, redact: bool) -> str:
    """The name shown for an item, pseudonymised under ``--redact`` (FR-043).

    The pseudonym is a hash of the identity, so it is stable across runs and machines and the
    same file lines up between two reports. The **extension is preserved on purpose**: the
    claim under dispute is about `.md` files specifically, so redacting it away would destroy
    the argument the report exists to settle, while leaking nothing about the path.
    """
    if not redact:
        return rollup.identity
    digest = sha256(rollup.item_id.encode("utf-8")).hexdigest()[:PSEUDONYM_HEX_DIGITS]
    suffix = PurePosixPath(rollup.identity).suffix
    return f"{PSEUDONYM_PREFIX}{digest}{suffix}"


def _uncertainty_notes(analyses: Sequence[SessionAnalysis], policy: str) -> list[str]:
    """What a reader must be told wherever these totals appear (FR-097).

    Three dominate, and they are stated first: the prices are imputed, the shared carry rests
    on a policy the reader can change, and some resident content never reaches the transcript.
    Each session's own limitations follow, deduplicated in encounter order.
    """
    # Two of the three are already stated by the analysis itself — that the prices are imputed
    # and that some resident content never reaches the transcript — and they are taken from
    # there rather than re-typed, so there is one wording of each. The policy is the third and
    # is the only one this layer knows about, because it is a presentation-level choice the
    # reader can change.
    policy_note = (
        f"Shared carry cost is divided by the '{policy}' policy: {describe_policy(policy)} "
        f"A different policy moves per-item figures without changing the total."
    )
    return _dedup([note for analysis in analyses for note in analysis.limitations] + [policy_note])


def _limitations(
    analyses: Sequence[SessionAnalysis],
    threshold_gaps: Sequence[str],
    rollups: Sequence[_ItemRollup],
) -> list[str]:
    """Required output, not optional garnish (FR-018)."""
    notes = [note for analysis in analyses for note in analysis.limitations]
    notes.append(
        "Content too small to cache is charged as fresh input at full rate every turn; "
        "that cost is reported as conversation overhead rather than charged to an item, "
        "because "
        "separating it needs per-turn cache-lane classification."
    )
    if threshold_gaps:
        notes.append(
            f"The minimum cacheable size is not recorded for "
            f"{', '.join(threshold_gaps)}, so items were not checked for cacheability on "
            f"{'that model' if len(threshold_gaps) == 1 else 'those models'}."
        )
    flagged = sorted({model for rollup in rollups for model in rollup.never_cacheable_on})
    if flagged:
        notes.append(
            f"Some items are smaller than the minimum cacheable size on "
            f"{', '.join(flagged)}, where they are charged at full rate every turn rather "
            f"than at the cache-read rate."
        )
    return _dedup(notes)


def _weakest(current: str, candidate: str, ladder: Sequence[str]) -> str:
    """The weaker of two ladder values — ladders run strongest-first."""
    return ladder[max(ladder.index(current), ladder.index(candidate))]


def _share(part: int, total: int) -> float:
    """A share of the total. Every absolute is paired with one (FR-011)."""
    if total == 0:
        return 0.0
    return part / total


def _dedup(values: Sequence[str]) -> list[str]:
    """Order-preserving dedup — deterministic, unlike a set (FR-017)."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique
