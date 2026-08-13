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
figures, and policy descriptions are read from :mod:`claude_cost_tracker.config.components` and
:mod:`claude_cost_tracker.model.policy`. A label re-typed here would be a second source of truth
(Principle IX, FR-016).

**Deterministic to the byte.** Same analyses in, same payload out, on every machine (FR-017,
SC-009). Every list is sorted explicitly; nothing is emitted in set or dict-insertion order.
The one exception is ``generated_at``, which is a clock reading and can be pinned by the
caller.

A section is emitted empty only where the analysis genuinely cannot support it — an absent
section is honest, a fabricated one is not. ``diagnostics.anchor_reconciliation`` is the one
that remains so.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, NamedTuple

from claude_cost_tracker import __version__
from claude_cost_tracker.analyse import ReportInput
from claude_cost_tracker.config import (
    ATTRIBUTION_COMPONENTS,
    CHARGE_COMPONENTS,
    sig_figs_for,
)
from claude_cost_tracker.config.categories import (
    INJECTED_ITEM_DESCRIPTIONS,
    MIXED_CATEGORY,
    injected_name,
)
from claude_cost_tracker.config.components import (
    BASIS_VALUES,
    CONFIDENCE_VALUES,
    attribution_component,
)
from claude_cost_tracker.ingest.discover import SHORT_ID_LENGTH
from claude_cost_tracker.ingest.tokens import (
    CHARACTERS_PER_TOKEN_ESTIMATE,
    CHARACTERS_PER_TOKEN_RANGE,
)
from claude_cost_tracker.model.policy import describe as describe_policy
from claude_cost_tracker.model.reconcile import ReconciliationError
from claude_cost_tracker.model.residency import ItemPart
from claude_cost_tracker.money import allocate

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

# The node a consumer keys on to draw the remainder as "not one of the named things" rather
# than as a folder (FR-040). The value is part of the payload contract, not a chart detail.
UNATTRIBUTED_PATH = "unattributed"

# Shown instead of a project name when the transcript never recorded which directory Claude
# Code was started from. Named rather than left blank so a row is never silently unlabelled.
UNRECORDED_PROJECT = "(project not recorded)"
TREE_ROOT_PATH = "/"

# Which field of a priced turn each charge component reads. A component added to the registry
# with no field here raises a `KeyError` naming it, rather than being silently omitted.
CHARGE_FIELDS: dict[str, str] = {
    "fresh_input": "fresh_input_micros",
    "cache_write": "cache_write_micros",
    "cache_read": "cache_read_micros",
    "output": "output_micros",
}

# Item kinds that are resident instruction content — present from the start of the session and
# charged on every turn — as opposed to a file that was read while doing work (FR-037, §6).
INSTRUCTION_KINDS: dict[str, str] = {
    "instruction_file": "Instruction files",
    "system_prompt": "Base instructions",
    "skill": "Skills",
    "tool_schema": "Tool and MCP schemas",
    "mcp_schema": "Tool and MCP schemas",
}

# The file names Claude Code loads as instruction content on every session. The category
# registry classifies these as `docs`, which is the right answer for a per-category table and
# the wrong one here: the whole point of the comparison is to separate an instruction file from
# any other `.md`, a distinction finer than the category registry draws. Kept deliberately
# narrow so it cannot drift into a second file-classification scheme.
INSTRUCTION_FILE_NAMES: frozenset[str] = frozenset({"claude.md", "agents.md"})
SKILL_CATEGORY = "skill"

# A path embedded in a sentence: a run of non-space, non-quote characters containing a
# separator. Used only to pseudonymise one, never to interpret one.
_PATH_IN_TEXT = re.compile(r"[^\s'\"]*/[^\s'\"]*")


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
    # Carried through from the model for composite items (the skill catalogue). Merging two
    # rollups keeps the richer listing, so a session that saw more skills describes the item.
    parts: tuple[ItemPart, ...] = ()
    # Token-turns this item spent in each of the two lanes that carry cost is charged in. They
    # are the weights the carry figure is divided by to say how much of it was paid at the 0.1x
    # cache rate and how much at full rate (see `_lane_micros`).
    cached_token_turns: int = 0
    uncached_token_turns: int = 0

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

# What each dimension means, in the words a reader needs to choose between them. Defined here
# beside the dimensions themselves (Principle IX) and rendered wherever the choice is offered:
# a control whose options have to be guessed is one people pick at random and then misread.
#
# `item` and `file` differ only where one name covers more than one thing — a path that could
# not be resolved and so means something different per project, or an injected item being shown
# per project. On a corpus where every path resolved, they produce the same rows, and saying so
# is more useful than implying a distinction the reader will go looking for.
GROUPING_DESCRIPTIONS: dict[str, str] = {
    "item": (
        "One row per distinct thing that was in context, exactly as recorded — the ungrouped "
        "view. Two things with the same name stay apart here: an unresolved path that means a "
        "different file in each project, or injected content being shown per project. Where "
        "every path resolved, this and 'file' show the same rows."
    ),
    "file": (
        "One row per name, merging anything that shares it. Differs from 'item' only where the "
        "same name covered more than one thing — otherwise the two are identical."
    ),
    "folder": (
        "One row per directory, holding the files sitting *directly* in it. Not everything "
        "beneath it: a file counted once for every folder above it would be counted many times "
        "in one table. The folder tree further down shows the everything-beneath figure."
    ),
    "ext": (
        "One row per file extension — .md against .py against .json. The view that answers "
        "'what kind of file is this spend going to'."
    ),
    "category": (
        "One row per category — docs, source, specs, tool schemas. The coarsest view, and the "
        "one that fits on a slide."
    ),
}
DEFAULT_GROUPING = "item"

# Ranking measures (contracts/cli.md). Sorting only reorders rows — it can never change what
# is in the table or what it sums to, which is why no reconciliation assertion depends on it.
SORTS: tuple[str, ...] = ("cost", "carry", "direct", "reads", "share")
DEFAULT_SORT = "cost"

_SORT_KEYS = {
    "cost": lambda r: r.total_micros,
    "share": lambda r: r.total_micros,  # a share is the total over a constant; same ordering
    "carry": lambda r: r.carry_micros,
    "direct": lambda r: r.direct_micros,
    "reads": lambda r: r.reads,
}


# Above these, a list stops informing and starts burying the figures underneath it.
SESSION_LIST_LIMIT = 6
VERSION_LIST_LIMIT = 6


def session_display_name(session_id: str, title: str | None) -> str:
    """How a session is named on every surface: its name, then enough id to select it.

    One function so the terminal, the report, the UI, and the notebook cannot drift into three
    spellings of the same thing (Principle IX). A session Claude Code never named falls back to
    the id fragment rather than to an invented name.
    """
    short = session_id[:SHORT_ID_LENGTH]
    return f"{title} ({short})" if title else short


def summarise_ids(ids: Sequence[str]) -> str:
    """Name a handful of sessions; count a corpus.

    A whole-machine sweep printed nine hundred UUIDs before the first figure. Nobody reads
    them, and burying the total under three screens of identifiers is a worse answer than the
    count — the ids stay in the payload for anyone who wants them, and `ccost sessions` lists
    them. Below the cutoff the names are more useful than the number, so both forms exist and
    the size of the selection picks between them.
    """
    if len(ids) <= SESSION_LIST_LIMIT:
        return ", ".join(ids)
    shown = ", ".join(ids[:SESSION_LIST_LIMIT])
    return f"{len(ids)} sessions ({shown}, and {len(ids) - SESSION_LIST_LIMIT} more)"


def summarise_versions(versions: Sequence[str]) -> str:
    """The span of Claude Code versions, as a range once a full list stops being readable."""
    if len(versions) <= VERSION_LIST_LIMIT:
        return ", ".join(versions)
    return f"{len(versions)} versions from {versions[0]} to {versions[-1]}"


def forced_reload_micros(data: Mapping[str, Any]) -> int:
    """Cost charged to an invalidation event rather than to any item.

    When a prefix-tier change re-writes the whole prompt, the re-write is charged to the change
    that caused it rather than smeared over the content it re-wrote (FR-081). That makes it a
    fifth kind of line: attributed, but not to a file. Every surface has to give it its own row,
    because the item rows plus the remainder are otherwise short by exactly this much — a
    breakdown that does not add up (Principle X, invariant A1).
    """
    return sum(int(event["forced_reload_micros"]) for event in data.get("invalidations", ()))


class UnknownGroupingError(ValueError):
    """An unsupported `--by` dimension. Never silently falls back to the default."""


class UnknownSortError(ValueError):
    """An unsupported `--sort` measure. Never silently falls back to the default."""


def build_report_data(
    analyses: Sequence[ReportInput],
    *,
    redact: bool = False,
    sessions_excluded_count: int = 0,
    sessions_skipped: Sequence[str] = (),
    generated_at: str | None = None,
    group_by: str = DEFAULT_GROUPING,
    sort_by: str = DEFAULT_SORT,
    merge_injected: bool = True,
) -> dict[str, Any]:
    """Build the report-data payload for one or more analysed sessions.

    ``group_by`` merges the item rows along one dimension (FR-007). Grouping only ever *merges*
    rows, so every dimension sums to the same attributed total — a grouping that changed the
    total would mean it had dropped or duplicated something.

    Raises :class:`~claude_cost_tracker.model.reconcile.ReconciliationError` if the payload would not add
    up, and ``ValueError`` for an empty selection or a mix of carry-splitting policies —
    figures produced under different policies are not comparable and must not be summed.
    """
    if group_by not in GROUPINGS:
        raise UnknownGroupingError(f"unknown grouping {group_by!r}; known: {list(GROUPINGS)}")
    if sort_by not in SORTS:
        raise UnknownSortError(f"unknown sort measure {sort_by!r}; known: {list(SORTS)}")
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
    if merge_injected:
        collapsed = _collapse_injected(rollups)
        # Collapsing is a merge like any other, so it owes the same proof: it may only join
        # rows, never drop or duplicate one (FR-007, Principle X).
        _assert_grouping_conserves(rollups, collapsed, "injected")
        rollups = collapsed

    def rows_for(dimension: str) -> list[dict[str, Any]]:
        merged = _regroup(rollups, dimension)
        _assert_grouping_conserves(rollups, merged, dimension)
        # Item id breaks ties so the order is total, not merely sorted — two rows with equal
        # cost must not swap between runs (FR-017, SC-009).
        merged = sorted(merged, key=lambda r: (-_SORT_KEYS[sort_by](r), r.item_id))
        return [_item_payload(rollup, totals["cost_micros"], redact=redact) for rollup in merged]

    # Every dimension is built, not just the one asked for, so each section of the report can
    # be regrouped where it stands without a round trip and without the browser recomputing a
    # figure (Principle X): it only ever chooses which precomputed set of rows to show.
    items_by_grouping = {dimension: rows_for(dimension) for dimension in GROUPINGS}
    items = items_by_grouping[group_by]

    # Turn ordinals and residency spans share one axis across a multi-session selection, so the
    # sessions are ordered once, by id, and every turn index is offset from there.
    ordered = _ordered_sessions(analyses)
    attribution = _attribution(analyses, totals["cost_micros"])
    invalidations = _invalidations(analyses, redact=redact)
    # Follows the same merge/split choice as the table above it. Left unqualified while the
    # table was split, three projects' spans all rendered as "Skill listing" and the chart
    # folded them into one "Skill listing x3" bar — the page saying "merged" in the one place
    # the reader had just asked for them apart.
    residency, unbroken_spans = _residency(ordered, redact=redact, qualify_scope=not merge_injected)
    limitations = _limitations(analyses, threshold_gaps, rollups, unbroken_spans)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "tool_version": __version__,
        "cost_basis": COST_BASIS,
        "currency": CURRENCY,
        "policy": policy,
        "group_by": group_by,
        "sort_by": sort_by,
        "redacted": redact,
        "scope": _scope(analyses, sessions_excluded_count, sessions_skipped),
        "totals": {**totals, "uncertainty_notes": _uncertainty_notes(analyses, policy)},
        "components": _components(analyses, totals["cost_micros"]),
        # How the charges were *concluded* to divide up, as opposed to how they were incurred.
        # It carries the two conclusions that are never charged to a file — the conversation
        # itself and what the model wrote back — without which a per-item table plus the
        # remainder does not reach the total, and a reader is left with a silent gap.
        "attribution": attribution,
        "items": items,
        # The same rows merged every other way. `items` is one of these by reference, not a
        # copy: the terminal and `--json` keep reading the single dimension they asked for.
        "items_by_grouping": items_by_grouping,
        # Forced reloads, charged to the change that caused them rather than to the content
        # they re-wrote (FR-081). This is what answers "what did adding that server cost me?"
        "invalidations": invalidations,
        # The same money as `attribution`, arranged by where in the tree it landed. Built from
        # the ungrouped rollups so the hierarchy is over files whatever `--by` was asked for.
        "tree": _tree(rollups, attribution, invalidations, totals, redact=redact),
        "turns": _turns(ordered),
        "residency": residency,
        "comparison": _comparison(rollups, totals["cost_micros"], redact=redact),
        # Per session, so a multi-session selection can be read as "which sessions cost what"
        # rather than only as one merged total. Empty for a single session, where the section
        # would restate the headline.
        "sessions": _sessions(ordered, totals["cost_micros"]),
        "diagnostics": {
            "unparseable_records": sum(a.parsed.unparseable_count for a in analyses),
            # Anchor reconciliation is parsed (ingest/anchors.py) but not yet wired into
            # ReportInput; an empty list says "not checked", never "checked and clean".
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
    # A forced reload is `direct` cost charged to the *change* that caused it rather than to
    # any file (FR-081), so it is part of the direct total but never part of any item row. It
    # has to be added back here, and shown as its own line, or the printed table falls short
    # of the total with nothing explaining the gap.
    reload_direct = sum(event["forced_reload_micros"] for event in payload["invalidations"])
    concluded_by_id = {c["id"]: c["cost_micros"] for c in payload["attribution"]}
    if item_direct + reload_direct != concluded_by_id["direct"] or (
        item_carry != concluded_by_id["carry"]
    ):
        raise ReconciliationError(
            f"per-item figures do not partition the item-level conclusions: direct "
            f"{item_direct} vs {concluded_by_id['direct']}, carry {item_carry} vs "
            f"{concluded_by_id['carry']}"
        )
    _assert_turns_add_up(payload)
    _assert_tree_adds_up(payload["tree"], totals["cost_micros"])
    _assert_comparison_adds_up(payload)


def _assert_turns_add_up(payload: dict[str, Any]) -> None:
    """Every charge belongs to a turn, so the per-turn figures are the session total.

    A cumulative curve whose last point is not 100% of the total it is drawn against is a
    figure contradicting its own total, in the surface where that is hardest to notice.
    """
    charged = sum(turn["cost_micros"] for turn in payload["turns"])
    total = payload["totals"]["cost_micros"]
    if charged != total:
        raise ReconciliationError(
            f"per-turn figures sum to {charged} against a session total of {total}. Every "
            f"charge belongs to exactly one turn."
        )


def _assert_tree_adds_up(node: Mapping[str, Any], total_micros: int) -> None:
    """A node's children plus its own cost are the whole of it, at every level (FR-034).

    The same promise as the flat table, one dimension further in. A hierarchy that does not
    partition is read as structure rather than as arithmetic, so nobody checks it by eye.
    """
    if node["total_micros"] != total_micros:
        raise ReconciliationError(
            f"the cost tree covers {node['total_micros']} against a session total of "
            f"{total_micros}. A part-to-whole view must be the whole."
        )
    _assert_node_adds_up(node)


def _assert_node_adds_up(node: Mapping[str, Any]) -> None:
    children: Sequence[Mapping[str, Any]] = node["children"]
    covered = node["flat_micros"] + sum(child["total_micros"] for child in children)
    if covered != node["total_micros"]:
        raise ReconciliationError(
            f"tree node {node['path']!r} does not add up: {node['flat_micros']} of its own "
            f"plus {covered - node['flat_micros']} below it != {node['total_micros']}"
        )
    for child in children:
        _assert_node_adds_up(child)


def _assert_comparison_adds_up(payload: dict[str, Any]) -> None:
    """The two series plus anything unassignable are exactly the per-item figures (FR-037).

    The comparison is a re-arrangement of the item rows, never a re-measurement: if the sides
    do not sum back to the items, one side has silently absorbed or dropped a file — which is
    precisely the error the chart exists to rule out.
    """
    comparison = payload["comparison"]
    covered = sum(
        entry["cost_micros"]
        for series in comparison.values()
        if isinstance(series, list)
        for entry in series
    )
    items_total = sum(item["total_micros"] for item in payload["items"])
    if covered != items_total:
        raise ReconciliationError(
            f"the instruction-versus-reads comparison covers {covered} against {items_total} "
            f"attributed to items. Every item belongs to exactly one side, or to neither "
            f"explicitly."
        )


def _totals(analyses: Sequence[ReportInput]) -> dict[str, Any]:
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


def _scope(
    analyses: Sequence[ReportInput], excluded: int, skipped: Sequence[str] = ()
) -> dict[str, Any]:
    versions: set[str] = set()
    for analysis in analyses:
        versions |= analysis.parsed.producing_versions
    return {
        "sessions_included": sorted(a.session_id for a in analyses),
        # Names beside the ids. A wall of UUIDs says nothing about which session was which, and
        # the name is the only thing that does; the id fragment is what selects it. Ordered to
        # match `sessions_included` so a consumer can zip the two.
        "session_names": [
            session_display_name(a.session_id, getattr(a, "title", None))
            for a in sorted(analyses, key=lambda item: item.session_id)
        ],
        # Exclusion is part of the result, never a hidden input (FR-063).
        "sessions_excluded_count": excluded,
        # Sessions a sweep could not price at all — a foreign model in a shared ~/.claude. They
        # are named rather than counted: "3 skipped" tells a reader nothing they can act on.
        "sessions_skipped": list(skipped),
        "covered_through_turn": sum(len(a.timeline.turns) for a in analyses),
        "provisional": any(a.provisional for a in analyses),
        "producing_versions": sorted(versions),
    }


def _components(analyses: Sequence[ReportInput], total_micros: int) -> list[dict[str, Any]]:
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


def _invalidations(analyses: Sequence[ReportInput], *, redact: bool) -> list[dict[str, Any]]:
    """Prefix changes that forced content back into the cache, and what each cost.

    The `detail` is deliberately user-facing ("MCP server 'playwright' added") rather than a
    tier number: the finding is "adding that server cost $X in forced re-writes", not
    "CLAUDE.md got more expensive" (FR-081).
    """
    rows = [
        {
            "session_id": analysis.session_id,
            "turn": event.turn_index,
            "tier": event.tier,
            "trigger": event.trigger,
            "detail": _redact_paths(event.detail) if redact else event.detail,
            "forced_reload_micros": event.forced_reload_micros,
            "items_reloaded": event.items_reloaded,
            "basis": event.basis,
            "confidence": event.confidence,
        }
        for analysis in analyses
        for event in analysis.attribution.invalidations
    ]
    return sorted(rows, key=lambda row: (row["session_id"], row["turn"]))


def _redact_paths(detail: str) -> str:
    """Pseudonymise any path inside a user-facing sentence (FR-043).

    An invalidation detail names the thing that changed, and for the ``system`` tier that thing
    is a file — so the sentence carries a path that the rest of the payload has already been
    careful to remove. The sentence keeps its shape and its extension, for the same reason an
    item's pseudonym does: the finding must stay checkable once the path is gone.
    """
    return _PATH_IN_TEXT.sub(
        lambda match: f"{_pseudonym(match.group(0))}{PurePosixPath(match.group(0)).suffix}",
        detail,
    )


def _attribution(analyses: Sequence[ReportInput], total_micros: int) -> list[dict[str, Any]]:
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


def _ordered_sessions(analyses: Sequence[ReportInput]) -> list[tuple[ReportInput, int]]:
    """The sessions in a fixed order, each with the turn ordinal it starts at.

    A multi-session selection is drawn on one turn axis, so the ordinals have to be assigned
    once and shared by every section that names a turn. Ordering by session id rather than by
    argument order is what keeps the axis identical between two runs (FR-017).
    """
    ordered: list[tuple[ReportInput, int]] = []
    offset = 0
    for analysis in sorted(analyses, key=lambda analysis: analysis.session_id):
        ordered.append((analysis, offset))
        offset += len(analysis.timeline.turns)
    return ordered


def _turns(ordered: Sequence[tuple[ReportInput, int]]) -> list[dict[str, Any]]:
    """What each turn cost, in order, with compaction boundaries marked (FR-039).

    ``prompt_tokens`` is all three input measures, never ``input_tokens`` alone (FR-083): a
    session showing 4K fresh input after hours of work is not a small session, and reporting
    the uncached remainder as the conversation size is the trap that hides the carry cost this
    tool exists to price.
    """
    rows: list[dict[str, Any]] = []
    for analysis, offset in ordered:
        charges = analysis.attribution.charges
        turns = analysis.timeline.turns
        if len(charges) != len(turns):
            raise ReconciliationError(
                f"session {analysis.session_id}: {len(charges)} priced turns against "
                f"{len(turns)} in the timeline. A per-turn figure would name the wrong turn."
            )
        compactions = _compaction_by_turn(analysis)
        for turn_index, turn in enumerate(turns):
            charge = charges[turn_index]
            rows.append(
                {
                    "ordinal": offset + turn_index + 1,
                    "session_id": analysis.session_id,
                    "model": charge.model,
                    "cost_micros": charge.total_micros,
                    "components": {
                        component.id: getattr(charge, CHARGE_FIELDS[component.id])
                        for component in CHARGE_COMPONENTS
                    },
                    "prompt_tokens": turn.usage.prompt_tokens,
                    "compaction": compactions.get(turn_index),
                }
            )
    return rows


def _compaction_by_turn(analysis: ReportInput) -> dict[int, dict[str, Any]]:
    """The compaction boundaries, keyed by the turn they landed on.

    Measured, not inferred: the boundary record states the conversation size before and after
    itself, so what a compaction dropped is read off the record (pass-2 §2.1) rather than
    estimated from what stopped appearing.
    """
    turns = analysis.timeline.compaction_turns
    if not analysis.timeline.turns:
        return {}
    records = analysis.parsed.compactions
    if len(records) != len(turns):
        raise ReconciliationError(
            f"session {analysis.session_id}: {len(records)} compaction records against "
            f"{len(turns)} boundaries placed on the timeline; a boundary would be reported on "
            f"the wrong turn."
        )

    boundaries: dict[int, dict[str, Any]] = {}
    for record, turn_index in zip(records, turns, strict=True):
        existing = boundaries.get(turn_index)
        if existing is None:
            boundaries[turn_index] = {
                "occurred": True,
                "pre_tokens": record.pre_tokens,
                "post_tokens": record.post_tokens,
                "dropped_tokens": record.dropped_tokens,
            }
            continue
        # Two boundaries between the same pair of turns are one event as far as a per-turn
        # figure can tell: the interval starts at the first size and ends at the last.
        existing["post_tokens"] = record.post_tokens
        existing["dropped_tokens"] += record.dropped_tokens
    return boundaries


def _residency(
    ordered: Sequence[tuple[ReportInput, int]], *, redact: bool, qualify_scope: bool = False
) -> tuple[list[dict[str, Any]], int]:
    """One row per residency span, with its per-turn lane (FR-036).

    Returns the rows and the number of spans left without a per-turn breakdown. A file read
    once looks cheap in a leaderboard; the same file sitting in context for ninety turns is
    charged on every one of them, and the span is what makes that length legible.
    """
    rows: list[dict[str, Any]] = []
    unbroken = 0
    for analysis, offset in ordered:
        timeline = analysis.timeline
        if not timeline.turns:
            continue
        lanes = _lanes_by_item(analysis)
        for span in sorted(timeline.spans, key=lambda span: (span.first_turn, span.item_id)):
            item = timeline.items[span.item_id]
            end_turn = timeline.final_turn_index if span.last_turn is None else span.last_turn
            by_turn = lanes.get(span.item_id, {})
            classified = [by_turn.get(index) for index in range(span.first_turn, end_turn + 1)]
            if any(lane is None for lane in classified):
                # A turn whose records support no lane cannot be drawn as one: every lane is a
                # different rate, so a filler value would be a price claim. The span keeps its
                # length and loses its breakdown, and the count is stated in the limitations.
                unbroken += 1
                classified = []
            display = _display_for(
                item.item_id, item.identity, redact=redact, qualify_scope=qualify_scope
            )
            rows.append(
                {
                    "session_id": analysis.session_id,
                    "item_id": f"{item.kind}:{display}" if redact else item.item_id,
                    "display": display,
                    # Turn ordinals, one-based and on the shared axis, so a span lines up with
                    # the turn rows that priced it.
                    "first_turn": offset + span.first_turn + 1,
                    "last_turn": None if span.last_turn is None else offset + span.last_turn + 1,
                    "weight_tokens": item.size_tokens,
                    "end_reason": span.end_reason,
                    "lane_by_turn": classified,
                }
            )
    return rows, unbroken


def _lanes_by_item(analysis: ReportInput) -> dict[str, dict[int, str]]:
    """Every lane verdict, indexed by item and turn — one pass instead of a scan per span."""
    lanes: dict[str, dict[int, str]] = {}
    for assignment in analysis.attribution.lanes.assignments:
        lanes.setdefault(assignment.item_id, {})[assignment.turn_index] = assignment.lane
    return lanes


def _rollups(analyses: Sequence[ReportInput]) -> tuple[list[_ItemRollup], list[str]]:
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
                    parts=item.parts,
                )
                rollups[item.item_id] = rollup
            else:
                # The same file in two sessions: the larger observed size is the one carried.
                rollup.size_tokens = max(rollup.size_tokens, item.size_tokens)
                # And the fuller listing is the one that describes it — a session that had more
                # skills available knows about entries the other never saw.
                if len(item.parts) > len(rollup.parts):
                    rollup.parts = item.parts

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

        _fold_lanes(analysis, rollups)
        threshold_gaps.update(analysis.attribution.lanes.threshold_unknown_models)

    return (
        sorted(rollups.values(), key=lambda r: (-r.total_micros, r.item_id)),
        sorted(threshold_gaps),
    )


@dataclass
class _TreeNode:
    """One node while the hierarchy is being built, before it is priced and sorted."""

    name: str
    path: str
    flat_micros: int = 0
    # The precision this node's own figure supports; ``None`` until it has one of its own.
    sig_figs: int | None = None
    children: dict[str, "_TreeNode"] = field(default_factory=dict)

    def child(self, key: str, *, name: str, path: str) -> "_TreeNode":
        node = self.children.get(key)
        if node is None:
            node = _TreeNode(name=name, path=path)
            self.children[key] = node
        return node


def _tree(
    rollups: Sequence[_ItemRollup],
    attribution: Sequence[dict[str, Any]],
    invalidations: Sequence[dict[str, Any]],
    totals: Mapping[str, Any],
    *,
    redact: bool,
) -> dict[str, Any]:
    """Cost over the folder tree, rooted at the session total (FR-034, FR-040).

    Two decisions are worth stating, because both are visible in the picture:

    **The root is the whole session, not just the files.** A part-to-whole view whose whole is
    only the attributed part shows a full-width root bar that silently stands for a fraction of
    the money. So the conclusions that are never charged to a file — the conversation itself,
    what the model wrote back, the reloads charged to the change that forced them — sit at the
    root as named siblings of the folders, alongside the unattributed remainder.

    **Items with no path get a named bucket, not a folder.** A skill listing or a tool-schema
    delta is not in a directory, and inventing one for it would put a fabricated folder in a
    hierarchy the reader is meant to recognise. They are collected under ``(skill)``,
    ``(tool_schema)`` and so on at the root — the same convention the flat per-folder grouping
    already uses for them (`_group_key`), so the two views name them identically.
    """
    root = _TreeNode(name=TREE_ROOT_PATH, path=TREE_ROOT_PATH)
    for rollup in rollups:
        parent = root
        for segment, real_path in _folder_chain(rollup):
            name = _pseudonym(real_path) if redact else segment
            parent = parent.child(real_path, name=name, path=_join(parent.path, name))
        display = _display_for(rollup.item_id, rollup.identity, redact=redact, qualify_scope=True)
        leaf_name = PurePosixPath(display).name or display
        leaf = parent.child(rollup.item_id, name=leaf_name, path=_join(parent.path, leaf_name))
        leaf.flat_micros += rollup.total_micros
        leaf.sig_figs = sig_figs_for(rollup.confidence)

    root.children = {key: _compress(child) for key, child in root.children.items()}
    for node in _non_item_nodes(attribution, invalidations, totals):
        root.children[node.path] = node
    return _emit(root, int(totals["cost_micros"]))


def _folder_chain(rollup: _ItemRollup) -> list[tuple[str, str]]:
    """The directories an item hangs under, each with the real path that identifies it.

    The real path is the merge key even under redaction, so two files in the same directory
    still land in the same node when the node's *name* is a pseudonym.
    """
    parts = PurePosixPath(rollup.identity).parts
    if len(parts) <= 1:
        return [(f"({rollup.kind})", f"kind:{rollup.kind}")]

    chain: list[tuple[str, str]] = []
    cumulative = ""
    for segment in parts[:-1]:
        if segment == TREE_ROOT_PATH:
            # The filesystem root is the tree root; it is not a level of its own.
            continue
        cumulative = f"{cumulative}/{segment}"
        chain.append((segment, cumulative))
    return chain


def _non_item_nodes(
    attribution: Sequence[dict[str, Any]],
    invalidations: Sequence[dict[str, Any]],
    totals: Mapping[str, Any],
) -> list[_TreeNode]:
    """The root-level nodes for money that belongs to no file, remainder included."""
    concluded = {component["id"]: component for component in attribution}
    reload_micros = sum(event["forced_reload_micros"] for event in invalidations)
    candidates = [
        _TreeNode(
            # Charged to the change that forced the reload, never to the content re-written
            # (FR-081) — so it is a sibling of the folders, not a cost inside one.
            name="Forced reloads",
            path="(forced-reloads)",
            flat_micros=reload_micros,
            sig_figs=min(
                (sig_figs_for(event["confidence"]) for event in invalidations),
                default=sig_figs_for(CONFIDENCE_VALUES[-1]),
            ),
        ),
        _TreeNode(
            name=attribution_component("overhead").plain_name,
            path="(overhead)",
            flat_micros=concluded["overhead"]["cost_micros"],
            sig_figs=concluded["overhead"]["display_sig_figs"],
        ),
        _TreeNode(
            name=attribution_component("output").plain_name,
            path="(output)",
            flat_micros=concluded["output"]["cost_micros"],
            sig_figs=concluded["output"]["display_sig_figs"],
        ),
        _TreeNode(
            # Named in the glossary's plain language; the path is what a consumer keys on to
            # draw it as the remainder rather than as a folder (FR-040).
            name="Couldn't attribute",
            path=UNATTRIBUTED_PATH,
            flat_micros=int(totals["unattributed_micros"]),
            sig_figs=int(totals["display_sig_figs"]),
        ),
    ]
    return [node for node in candidates if node.flat_micros]


def _compress(node: _TreeNode) -> _TreeNode:
    """Fold a directory that has one child directory and no cost of its own into that child.

    ``/Users/me/projects/repo/src`` is five levels that branch nowhere and cost nothing by
    themselves; spending five of a reader's six visible levels on them buries the level that
    actually divides the money. No figure moves — only the number of rows it takes to show it.
    """
    node.children = {key: _compress(child) for key, child in node.children.items()}
    if len(node.children) != 1 or node.flat_micros:
        return node
    ((_, only),) = node.children.items()
    if not only.children:
        # A directory holding a single file still names the directory: that is the level the
        # reader asked for, and the file is already named on the row below.
        return node
    return _TreeNode(
        name=f"{node.name}/{only.name}",
        path=only.path,
        flat_micros=only.flat_micros,
        sig_figs=only.sig_figs,
        children=only.children,
    )


def _emit(node: _TreeNode, total_micros: int) -> dict[str, Any]:
    """Price a node bottom-up: its own cost plus everything below it, ranked and shared."""
    children = [_emit(child, total_micros) for child in node.children.values()]
    children.sort(key=lambda child: (-child["total_micros"], child["path"]))
    total = node.flat_micros + sum(child["total_micros"] for child in children)
    # A node is no more precise than the least precise figure inside it (FR-095).
    sig_figs = min(
        [node.sig_figs or sig_figs_for(CONFIDENCE_VALUES[0])]
        + [child["display_sig_figs"] for child in children]
    )
    return {
        "name": node.name,
        "path": node.path,
        "flat_micros": node.flat_micros,
        "total_micros": total,
        "share": _share(total, total_micros),
        "display_sig_figs": sig_figs,
        "children": children,
    }


def _join(parent_path: str, name: str) -> str:
    return f"{parent_path}{name}" if parent_path == TREE_ROOT_PATH else f"{parent_path}/{name}"


def _sessions(
    ordered: Sequence[tuple[ReportInput, int]], total_micros: int
) -> list[dict[str, Any]]:
    """One row per session in the selection: what it cost, and why it cost that.

    The split into loading and keeping is carried per session because it is the finding that
    differs between them — a short session that read a lot and a long one that held a little
    can reach the same total for opposite reasons, and the fix for each is the opposite too.

    Ordered by cost so the chart built from it ranks without the renderer having to sort, and
    so two runs over the same selection draw the same picture (FR-017).
    """
    rows: list[dict[str, Any]] = []
    for analysis, _offset in ordered:
        direct = sum(
            row.cost_micros
            for row in analysis.attribution.attributions
            if row.component == "direct"
        )
        carry = sum(
            row.cost_micros for row in analysis.attribution.attributions if row.component == "carry"
        )
        cost = analysis.reconciliation.total_micros
        rows.append(
            {
                "session_id": analysis.session_id,
                "title": getattr(analysis, "title", None),
                "display_name": session_display_name(
                    analysis.session_id, getattr(analysis, "title", None)
                ),
                "cost_micros": cost,
                "direct_micros": direct,
                "carry_micros": carry,
                # Whatever is neither: output, the conversation itself, and the remainder. Named
                # so the three parts add to the session total and the bar can be read as a whole.
                "other_micros": cost - direct - carry,
                "turns": len(analysis.timeline.turns),
                "share": _share(cost, total_micros),
                "provisional": analysis.provisional,
                "display_sig_figs": sig_figs_for(TOTALS_CONFIDENCE),
            }
        )
    rows.sort(key=lambda row: (-row["cost_micros"], row["session_id"]))
    return rows


def _comparison(
    rollups: Sequence[_ItemRollup], total_micros: int, *, redact: bool = False
) -> dict[str, Any]:
    """Always-resident instruction content against work-driven file reads (FR-037, §6).

    The question the tool was commissioned to settle splits in two, and the two halves have
    different answers, which is why they are drawn as two series on **one** axis rather than as
    two pie charts: instruction content is a fixed block charged on every turn from the start,
    while file reads accumulate as work happens. Only a common scale says which is bigger.

    Both series carry the same two measures — the tokens the content occupies and its
    API-equivalent cost — so they are directly comparable. What differs is *how* the cost is
    incurred, and the note says so rather than leaving the reader to assume the two are alike.
    """
    if not rollups:
        return {}

    buckets: dict[str, dict[str, list[int]]] = {
        "resident_instruction": {},
        "work_driven_reads": {},
        "unassigned": {},
    }
    # What each bar is made of. "Instruction files: $6.68" beside an $86 "Skills" bar reads as
    # "CLAUDE.md is not in here" — it *is*, and naming the members is the only way a reader can
    # see that. Kept alongside the totals rather than derived later, because the bucketing rule
    # lives here and re-deriving it in a renderer would be a second copy of it.
    members: dict[str, dict[str, list[dict[str, Any]]]] = {side: {} for side in buckets}
    for rollup in rollups:
        side, label = _comparison_side(rollup)
        entry = buckets[side].setdefault(label, [0, 0])
        entry[0] += rollup.size_tokens
        entry[1] += rollup.total_micros
        members[side].setdefault(label, []).append(
            {
                "name": _display_for(
                    rollup.item_id, rollup.identity, redact=redact, qualify_scope=True
                ),
                "cost_micros": rollup.total_micros,
                "tokens": rollup.size_tokens,
            }
        )

    comparison: dict[str, Any] = {
        side: _comparison_entries(buckets[side], total_micros, members[side])
        for side in ("resident_instruction", "work_driven_reads")
    }
    note = (
        "Both series are tokens and API-equivalent cost on one scale. They are not charged the "
        "same way: resident instruction content is charged on every turn of the session, while "
        "a file read is charged once when it is read and then on every turn it stays in "
        "context."
    )
    if buckets["unassigned"]:
        # Naming what would not divide is the point of the section. Folding it into whichever
        # side looked plausible is exactly the move that would make the answer unfalsifiable.
        comparison["unassigned"] = _comparison_entries(
            buckets["unassigned"], total_micros, members["unassigned"]
        )
        note += (
            " Content that is neither an instruction item nor a file read is listed separately "
            "under 'unassigned' rather than counted on either side."
        )
    comparison["note"] = note
    return comparison


def _comparison_side(rollup: _ItemRollup) -> tuple[str, str]:
    """Which side of the comparison an item belongs on, and under what label.

    Decided by what the item *is*, not by how much it cost. Instruction files are separated
    from other documentation deliberately: the disputed claim is about instruction content
    specifically, and lumping every ``.md`` together would answer a different question.
    """
    label = INSTRUCTION_KINDS.get(rollup.kind)
    if label is not None:
        return "resident_instruction", label
    if rollup.kind != "file":
        return "unassigned", f"{rollup.kind} content"
    if PurePosixPath(rollup.identity).name.lower() in INSTRUCTION_FILE_NAMES:
        return "resident_instruction", INSTRUCTION_KINDS["instruction_file"]
    if rollup.category == SKILL_CATEGORY:
        return "resident_instruction", INSTRUCTION_KINDS["skill"]
    return "work_driven_reads", rollup.category


def _comparison_entries(
    bucket: Mapping[str, Sequence[int]],
    total_micros: int,
    members: Mapping[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    ranked = sorted(bucket.items(), key=lambda pair: (-pair[1][1], pair[0]))
    return [
        {
            "label": label,
            "tokens": tokens,
            "cost_micros": micros,
            "share": _share(micros, total_micros),
            # The items behind the bar, dearest first. A label alone cannot tell a reader that
            # "Instruction files" is where their CLAUDE.md went.
            "members": sorted(
                (members or {}).get(label, []),
                key=lambda row: (-int(row["cost_micros"]), str(row["name"])),
            ),
        }
        for label, (tokens, micros) in ranked
    ]


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
                cached_token_turns=rollup.cached_token_turns,
                uncached_token_turns=rollup.uncached_token_turns,
            )
            continue

        _absorb(target, rollup, add_sizes=True)

    return sorted(merged.values(), key=lambda r: (-r.total_micros, r.item_id))


def _absorb(target: _ItemRollup, rollup: _ItemRollup, *, add_sizes: bool) -> None:
    """Fold one rollup's figures into another. The only way two rows ever become one.

    ``add_sizes`` distinguishes the two reasons to merge. Grouping puts *different* items in one
    bucket, so their sizes add. Collapsing one item's per-project copies puts the *same* content
    under one row, so the size is the largest observed, exactly as re-reading a grown file is
    handled — adding there would report a listing several times its real weight.
    """
    target.direct_micros += rollup.direct_micros
    target.carry_micros += rollup.carry_micros
    target.reads += rollup.reads
    target.turns_resident += rollup.turns_resident
    target.size_tokens = (
        target.size_tokens + rollup.size_tokens
        if add_sizes
        else max(target.size_tokens, rollup.size_tokens)
    )
    target.never_cacheable_on |= rollup.never_cacheable_on
    target.cached_token_turns += rollup.cached_token_turns
    target.uncached_token_turns += rollup.uncached_token_turns
    for session_id, cost in rollup.per_session.items():
        target.per_session[session_id] = target.per_session.get(session_id, 0) + cost
    # A merged row is only as trustworthy as its weakest member, and only as specific: two
    # categories in one bucket is "(mixed)", never whichever happened to arrive first.
    target.basis = _weakest(target.basis, rollup.basis, BASIS_VALUES)
    target.confidence = _weakest(target.confidence, rollup.confidence, CONFIDENCE_VALUES)
    if target.category != rollup.category:
        target.category = MIXED_CATEGORY
    if target.kind != rollup.kind:
        target.kind = MIXED_CATEGORY
    # The richer listing describes the merged item: a scope that saw more skills knows more
    # about what the listing contained than one that saw fewer.
    if len(rollup.parts) > len(target.parts):
        target.parts = rollup.parts


def _collapse_injected(rollups: list[_ItemRollup]) -> list[_ItemRollup]:
    """Merge each injected item's per-project copies into one row (FR-007).

    An item id is scoped by project so that two projects' `README.md` stay distinct files. That
    is right for files and wrong for what Claude Code injects: the skill listing is one thing a
    reader recognises, and seeing "Skill listing" three times — once per project the corpus
    touches — reads as a bug in the tool rather than as a fact about the corpus.

    Only injected identities collapse. Files keep their project scope, because two files with
    the same path in different projects really are two files.
    """
    merged: dict[str, _ItemRollup] = {}
    order: list[str] = []
    for rollup in rollups:
        collapsible = injected_name(rollup.identity) is not None
        key = f"{rollup.kind}:{rollup.identity}" if collapsible else rollup.item_id
        target = merged.get(key)
        if target is None:
            merged[key] = replace(
                rollup,
                item_id=key if collapsible else rollup.item_id,
                per_session=dict(rollup.per_session),
                never_cacheable_on=set(rollup.never_cacheable_on),
            )
            order.append(key)
            continue
        _absorb(target, rollup, add_sizes=False)

    return [merged[key] for key in order]


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


def _fold_lanes(analysis: ReportInput, rollups: dict[str, _ItemRollup]) -> None:
    """Fold one session's lane history into the item rollups (FR-078, SC-026).

    The lane classification names the models an item was **actually resident on and below the
    minimum for**, turn by turn. That is a materially narrower claim than comparing every
    item's size against every model the session used, which flags a file on a model it was
    never in context with — an over-approximation in the direction that looks like a finding.
    """
    for summary in analysis.attribution.lanes.summaries():
        rollup = rollups.get(summary.item_id)
        if rollup is None:
            # Resident but never attributed any cost: it has no row to carry the finding.
            continue
        rollup.cached_token_turns += summary.token_turns_by_lane.get("cached", 0)
        rollup.uncached_token_turns += summary.token_turns_by_lane.get("uncached", 0)
        rollup.never_cacheable_on.update(summary.never_cacheable_on)


def _item_payload(rollup: _ItemRollup, total_micros: int, *, redact: bool) -> dict[str, Any]:
    driver = _uncertainty_driver(rollup)
    display = _display_for(rollup.item_id, rollup.identity, redact=redact, qualify_scope=True)
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
        "lanes": _lane_micros(rollup),
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
        # What a composite item is made of. Empty for everything except the skill catalogue,
        # which is one cached block listing many skills — "Skills: $67" cannot answer *which*
        # skills, nor which of them arrive with a plugin and are not the reader's to change.
        "parts": _item_parts(rollup),
        # What this item *is*, for the few items that are not a file the reader can go and look
        # at. A row reading "skill_listing" names a record key, not a thing, and a reader who
        # cannot tell what a line item is cannot check the figure against it (Principle X).
        "what_it_is": INJECTED_ITEM_DESCRIPTIONS.get(rollup.identity, ""),
    }
    if not redact:
        payload["identity"] = rollup.identity
    return payload


def _item_parts(rollup: _ItemRollup) -> list[dict[str, Any]]:
    """Divide a composite item's cost across the things it lists.

    The split is by each entry's share of the listing text — measured, not modelled — and uses
    the same largest-remainder allocation as money, so the parts sum to the item exactly and
    the breakdown still reconciles (invariant A1).

    This is a *presentation* of one item, deliberately not a set of items. The listing is
    cached as one contiguous block, so its cacheability belongs to the whole; splitting it in
    the model pushed each 30-token entry below the minimum cacheable size and repriced it in
    another lane — the same session moved from $1.14 to $0.32 on nothing but that change.
    """
    parts = rollup.parts
    if not parts:
        return []
    shares = allocate(rollup.total_micros, [part.weight for part in parts])
    rows: list[dict[str, Any]] = [
        {
            "name": part.name,
            # Named as the reader would act on it: a plugin's skill is not theirs to edit.
            "origin": part.origin,
            "plugin": part.name.split(":", 1)[0] if part.origin == "plugin" else None,
            "cost_micros": micros,
            "share_of_item": micros / rollup.total_micros if rollup.total_micros else 0.0,
            "basis": BASIS_VALUES[1] if part.measured else BASIS_VALUES[-1],
        }
        for part, micros in zip(parts, shares, strict=True)
    ]
    rows.sort(key=lambda row: (-row["cost_micros"], row["name"]))
    return rows


def _lane_micros(rollup: _ItemRollup) -> dict[str, int]:
    """Split the item's cost across the three pricing lanes (cost-model §5.2).

    Direct cost is the write lane by construction — an item is charged ``direct`` exactly when
    it was being written into the cache. Carry cost spans **two** lanes: content served from
    cache at 0.1x, and content below the model's minimum cacheable prefix, which never enters
    the cache and is re-sent as fresh input at full rate every turn (``attribute.py`` charges
    that as carry, because it is the recurring cost of keeping the content there).

    The only observable weight to divide carry by is the token-turns the item spent in each
    lane, so that is what it is divided by — with a largest-remainder allocation, so the two
    lanes sum back to the carry figure exactly rather than to a rounded approximation of it.
    """
    weights = [rollup.cached_token_turns, rollup.uncached_token_turns]
    if rollup.carry_micros and not sum(weights):
        raise ReconciliationError(
            f"item {rollup.item_id!r} was charged {rollup.carry_micros} micro-dollars of carry "
            f"cost with no classified lane to explain it. Carry is only ever charged to an item "
            f"in the cached or the sub-threshold lane, so the two records disagree."
        )
    cached, uncached = allocate(rollup.carry_micros, weights)
    return {
        "cached_micros": cached,
        "uncached_micros": uncached,
        "loading_micros": rollup.direct_micros,
    }


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

    The width is the part of the figure the driver controls: the shared carry for a policy
    choice, the direct join otherwise. This is a *lower bound* on the true range — under the
    exclusive policy an item that was alone in context can be charged more than its
    proportional share — and it is deliberately not presented as a confidence interval, which
    would imply a distribution nobody measured.

    An estimated size is the one driver whose error is *characterised*, so it gets a real band
    rather than a symmetric guess. The figure comes from ``chars // 4``; the true ratio spans
    :data:`CHARACTERS_PER_TOKEN_RANGE`, so the cost scales by 4/5 to 4/3. It used to take the
    whole figure as the width, which reported a $45 item as "$0.00 to $89" — and $0.00 is a
    claim, not a caution: this content demonstrably occupied a cached block that was charged
    every turn. Overstating a range is as dishonest as understating one.
    """
    total = rollup.total_micros
    if driver == "size_estimate":
        low_ratio, high_ratio = CHARACTERS_PER_TOKEN_RANGE
        return {
            "low_micros": total * CHARACTERS_PER_TOKEN_ESTIMATE // high_ratio,
            "high_micros": total * CHARACTERS_PER_TOKEN_ESTIMATE // low_ratio,
        }
    width = rollup.carry_micros if driver == "carry_split_policy" else rollup.direct_micros
    return {"low_micros": max(0, total - width), "high_micros": total + width}


def _display_for(item_id: str, identity: str, *, redact: bool, qualify_scope: bool = False) -> str:
    """The name shown for an item, pseudonymised under ``--redact`` (FR-043).

    The pseudonym is a hash of the identity, so it is stable across runs and machines and the
    same file lines up between two reports. The **extension is preserved on purpose**: the
    claim under dispute is about `.md` files specifically, so redacting it away would destroy
    the argument the report exists to settle, while leaking nothing about the path.

    The single place a name is pseudonymised — the tree and the residency timeline name the
    same items, and two hashing rules would put the same file under two names.

    An injected item keeps its plain name even under redaction: it is the same content in every
    session on every machine, so it identifies nothing about this user, and hashing it would
    destroy a figure the reader needs while protecting nothing.
    """
    injected = injected_name(identity)
    if injected is not None:
        # Only a row that was *kept apart* is qualified, and only callers building rows ask for
        # it. The residency timeline names the same item from the un-collapsed model, where the
        # scope is always present — qualifying there would put a project suffix on every span
        # even when the table above it says the item is one thing.
        scope = _injected_scope(item_id) if qualify_scope else None
        if scope is None:
            return injected
        # The project path is a path like any other, so it is pseudonymised under --redact.
        return f"{injected} — {_pseudonym(scope) if redact else scope}"
    if not redact:
        return identity
    return f"{_pseudonym(item_id)}{PurePosixPath(identity).suffix}"


def _injected_scope(item_id: str) -> str | None:
    """The project an injected item's row belongs to, or ``None`` when it covers all of them.

    Reads the scope back out of the `kind:scope:identity` id minted in the residency model. A
    collapsed row has no scope segment and is not qualified at all.

    A row whose project was never recorded is qualified too, with that said out loud. Leaving
    it bare put an unlabelled "Skill listing" beside two labelled ones, which reads as a third
    thing nobody can identify rather than as the one fact available about it (Principle X:
    missing attribution is stated, not left blank).
    """
    parts = item_id.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[1] if parts[1] != "-" else UNRECORDED_PROJECT


def _pseudonym(text: str) -> str:
    """A stable, path-free stand-in for one identity or one path segment."""
    return f"{PSEUDONYM_PREFIX}{sha256(text.encode('utf-8')).hexdigest()[:PSEUDONYM_HEX_DIGITS]}"


def _uncertainty_notes(analyses: Sequence[ReportInput], policy: str) -> list[str]:
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
    return collapse_notes(
        [note for analysis in analyses for note in analysis.limitations] + [policy_note]
    )


def _limitations(
    analyses: Sequence[ReportInput],
    threshold_gaps: Sequence[str],
    rollups: Sequence[_ItemRollup],
    unbroken_spans: int,
) -> list[str]:
    """Required output, not optional garnish (FR-018)."""
    notes = [note for analysis in analyses for note in analysis.limitations]
    notes.append(
        "Content too small to cache is charged as fresh input at full rate every turn. That "
        "cost is charged to the item as carry cost and shown in its sub-threshold lane; the "
        "split between the two carry lanes is by token-turns, not separately measured."
    )
    if unbroken_spans:
        notes.append(
            f"{unbroken_spans} residency span(s) include turns whose records support no cache "
            f"lane, so those spans carry no per-turn lane breakdown rather than a guessed one."
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
    return collapse_notes(notes)


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


# Digits are what make two instances of the same limitation different strings: "spans versions
# 2.1.203, 2.1.204" and "spans versions 2.1.207, 2.1.220" are one limitation reported twice.
_DIGITS = re.compile(r"\d[\d,.]*")
# ...and a list of them is the same limitation as a longer list of them: "spans versions A, B"
# and "spans versions A, B, C" differ in how many, not in what they say.
_NUMBER_LIST = re.compile(r"#(?:[,\s]+#)+")


def collapse_notes(values: Sequence[str]) -> list[str]:
    """Dedup, then fold repeats of the *same* limitation into one line that says how many.

    Exact dedup is not enough across a corpus. Analysing 900 sessions produced 30 copies of
    "this session spans Claude Code versions ..." and 8 copies of "about N tokens left the
    conversation before a compaction", each differing only in its numbers — a page of notes
    that says four things. A reader scrolls past all of it, which means the limitations stop
    being read at exactly the scale where they matter most.

    Nothing is summed or averaged: inventing a combined figure from prose would be inventing a
    number. The first instance is shown verbatim and the rest are counted, so the reader learns
    the limitation, its shape, and how widespread it is.
    """
    groups: dict[str, list[str]] = {}
    for value in _dedup(values):
        template = _NUMBER_LIST.sub("#", _DIGITS.sub("#", value))
        groups.setdefault(template, []).append(value)
    collapsed = []
    for members in groups.values():
        if len(members) == 1:
            collapsed.append(members[0])
            continue
        collapsed.append(
            f"{members[0]} (and {len(members) - 1} more session(s) with the same limitation, "
            f"differing only in the figures)"
        )
    return collapsed


class SessionMetric(NamedTuple):
    """One rankable fact about a session, and what it means to a reader.

    The description is part of the registry rather than the template, because a column whose
    meaning has to be guessed is a column people rank by and then misread — "Reads" and
    ".md reads" are not the same question, and neither is ".md files".
    """

    key: str
    label: str
    cheap: bool
    description: str


# What the session picker can rank by. One registry, so the browser table, the notebook frame,
# and any future surface show the same columns under the same names (Principle IX). `cheap`
# marks the facts readable from the transcript's file metadata alone — those render instantly;
# the rest need the session analysed, and arrive when it is done.
SESSION_METRICS: tuple[SessionMetric, ...] = (
    SessionMetric(
        "records",
        "Records",
        True,
        "Lines in the transcript, including this session's subagents. A rough measure of how "
        "much happened, available without analysing anything.",
    ),
    SessionMetric(
        "bytes",
        "Size",
        True,
        "How large the transcript files are on disk. Not a cost: a big transcript can be "
        "cheap if little of it stayed in context.",
    ),
    SessionMetric(
        "cost_micros",
        "Cost",
        False,
        "API-equivalent cost estimate for the whole session — token counts priced at "
        "published list rates. Not a billed amount.",
    ),
    SessionMetric(
        "turns",
        "Rounds",
        False,
        "Request/response rounds with the model. Content is charged again on every round it "
        "stays in context, so this is what multiplies the cost of keeping anything loaded.",
    ),
    SessionMetric(
        "reads",
        "Reads",
        False,
        "How many times content was loaded into context across the session, counting every "
        "re-read of the same file separately.",
    ),
    SessionMetric(
        "md_reads",
        ".md reads",
        False,
        "The same count, restricted to markdown files — docs, specs, CLAUDE.md. Reading one "
        "file three times counts as three.",
    ),
    SessionMetric(
        "md_files",
        ".md files",
        False,
        "How many distinct markdown files were loaded. Reading one file three times counts "
        "as one — this is breadth where '.md reads' is repetition.",
    ),
    SessionMetric(
        "skills",
        "Skills",
        False,
        "Distinct skills the session actually pulled in. Not the skill listing, which is the "
        "menu of what was available and is present whether or not anything was invoked.",
    ),
    SessionMetric(
        "items",
        "Items",
        False,
        "How many distinct pieces of content — files, skills, injected schemas — were "
        "resident in context at some point during the session.",
    ),
)

CHEAP_SESSION_METRICS = tuple(metric.key for metric in SESSION_METRICS if metric.cheap)
ANALYSED_SESSION_METRICS = tuple(metric.key for metric in SESSION_METRICS if not metric.cheap)

_MARKDOWN_SUFFIX = ".md"


def session_facts(analysis: ReportInput) -> dict[str, int]:
    """The rankable facts about one analysed session (FR-060).

    Everything here is counted from the session's own timeline, so a fact and the figure it sits
    beside come from the same source and cannot disagree. Derived once, here, rather than in
    each surface that wants to sort by it.
    """
    timeline = analysis.timeline
    md_items = [
        item
        for item in timeline.items.values()
        if PurePosixPath(item.identity).suffix == _MARKDOWN_SUFFIX
    ]
    return {
        "cost_micros": analysis.reconciliation.total_micros,
        "turns": len(timeline.turns),
        "reads": sum(timeline.load_count(item_id) for item_id in timeline.items),
        "md_reads": sum(timeline.load_count(item.item_id) for item in md_items),
        "md_files": len(md_items),
        # The skills a session actually pulled in — not the listing, which is the menu of what
        # was available and is present whether or not anything was invoked. Keyed on the
        # *category*, because an invoked skill arrives as its SKILL.md file: `kind == "skill"`
        # matches only the listing itself, and counting that way reported 0 for every session.
        "skills": sum(
            1
            for item in timeline.items.values()
            if item.category == "skill" and injected_name(item.identity) is None
        ),
        "items": len(timeline.items),
    }
