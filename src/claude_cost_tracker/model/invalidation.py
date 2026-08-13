"""Prefix-tier changes that forced content to be re-loaded — charged to the change.

Caching is a **prefix match** over ``tools`` -> ``system`` -> ``messages``, in that render
order, so any byte change invalidates everything *after* it (docs/cost-model.md §3):

=========================================  =====  ======  ========
Change                                     tools  system  messages
=========================================  =====  ======  ========
Tool definitions added/removed/reordered    x       x        x
Model switch                                x       x        x
System prompt content (instruction files)   ok      x        x
Message content                             ok      ok       x
=========================================  =====  ======  ========

Instruction files live in the ``system`` tier, *after* the tool schemas. So adding one MCP
server mid-session forces **every instruction file to be re-written** at 1.25-2x. A naive tool
reports "CLAUDE.md got expensive". The honest finding is "adding that server cost $X in forced
re-writes" — which is why an invalidation is a first-class entity with its own cost, and why
that cost is charged to the change and never to the content re-loaded as a consequence
(FR-081).

**Attribute less rather than wrong.** The excess is the part of a turn's write charge that
newly-arriving content does not explain, and a turn-level join is loose: measured on real
transcripts the ratio of a turn's ``cache_creation`` to the size of the preceding tool result
has a median of **3.31** (§5.3). So the excess is bounded twice — by what the write actually
cost, and by what was even resident to be re-written — and the lower bound wins. Whatever is
left over stays in the visible unattributed remainder (FR-019).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from claude_cost_tracker.config.categories import categorize
from claude_cost_tracker.ingest.records import AttachmentRecord
from claude_cost_tracker.model.residency import Timeline

# Render order. The index of a tier is what makes invalidation *tiered*: a change at tier i
# invalidates tier i and everything after it, because the cache match is a prefix match.
TIERS: tuple[str, ...] = ("tools", "system", "messages")

TRIGGERS: tuple[str, ...] = ("tool_set_changed", "model_switched", "instruction_changed")

# Attachment types that change the tool/MCP schema block — the first tier, and therefore the
# most expensive thing to change mid-session.
_TOOL_TIER_ATTACHMENTS: frozenset[str] = frozenset({"deferred_tools_delta", "agent_listing_delta"})

# Attachment types that change resident instruction content, which renders in the system tier
# *after* the schemas.
_SYSTEM_TIER_ATTACHMENTS: frozenset[str] = frozenset({"skill_listing", "edited_text_file"})

# Categories whose content is instruction material rather than working files. An edited source
# file is ordinary message growth; an edited CLAUDE.md re-writes the system tier.
_INSTRUCTION_CATEGORIES: frozenset[str] = frozenset({"docs", "skill"})

# Most severe first. Two changes on one turn produce **one** event, never two, so the forced
# reload cannot be counted twice; the quieter change is named in the detail instead.
_TRIGGER_PRECEDENCE: tuple[str, ...] = ("model_switched", "tool_set_changed", "instruction_changed")

_MAX_NAMED = 3


@dataclass(frozen=True)
class TurnWrite:
    """What one turn was observed to pay for loading content in.

    Passed in rather than imported from :mod:`claude_cost_tracker.model.attribute` so this module stays a
    leaf: attribution composes lanes and invalidations, not the other way round.
    """

    turn_index: int
    write_micros: int
    write_tokens: int
    confidence_cap: str | None = None


@dataclass(frozen=True)
class InvalidationEvent:
    """A prefix-tier change that forced a re-write, with the cost it caused.

    ``tier`` is the **earliest** tier invalidated; :attr:`invalidated_tiers` derives the rest,
    because a prefix match invalidates everything after the change.
    """

    event_id: str
    turn_index: int
    tier: str
    trigger: str
    # The user-facing explanation: "MCP server 'playwright' added", never "tier 0 invalidated".
    # A cause a reader cannot act on is not a finding (Principle X).
    detail: str
    forced_reload_micros: int
    items_reloaded: int
    basis: str
    confidence: str
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"turn {self.turn_index}: unknown tier {self.tier!r}; known: {TIERS}")
        if self.trigger not in TRIGGERS:
            raise ValueError(
                f"turn {self.turn_index}: unknown trigger {self.trigger!r}; known: {TRIGGERS}"
            )
        if self.forced_reload_micros < 0:
            raise ValueError(
                f"turn {self.turn_index}: forced reload cost cannot be negative, got "
                f"{self.forced_reload_micros}"
            )

    @property
    def invalidated_tiers(self) -> tuple[str, ...]:
        """This tier and every tier rendered after it — the prefix-match consequence."""
        return TIERS[TIERS.index(self.tier) :]


def detect_invalidations(
    timeline: Timeline,
    attachments: Sequence[AttachmentRecord],
    writes: Sequence[TurnWrite],
    *,
    session_id: str,
) -> list[InvalidationEvent]:
    """Find prefix-tier changes between consecutive turns and cost the reload they forced.

    Only turns after the first can invalidate: the initial tool schemas and skill listing are
    a *first load*, not a change to something already cached.
    """
    write_by_turn = {write.turn_index: write for write in writes}
    duplicated = len(writes) - len(write_by_turn)
    if duplicated:
        raise ValueError(
            f"session {session_id}: {duplicated} duplicate turn index/indices in the write "
            f"ledger; each turn is charged once and a repeat would double-count the reload"
        )

    attachments_by_turn = _attachments_by_turn(timeline, attachments)

    events: list[InvalidationEvent] = []
    for turn_index in range(1, len(timeline.turns)):
        changes = _detect_changes(timeline, turn_index, attachments_by_turn.get(turn_index, ()))
        if not changes:
            continue
        event = _build_event(
            timeline, turn_index, changes, write_by_turn.get(turn_index), session_id
        )
        events.append(event)
    return events


def forced_reload_micros_at(events: Sequence[InvalidationEvent], turn_index: int) -> int:
    """Cost already charged to an invalidation on this turn.

    The caller subtracts this from the turn's write pool **before** splitting the remainder
    across what arrived, so a forced re-write never lands on the instruction files that were
    merely re-loaded as a consequence (FR-081).
    """
    return sum(event.forced_reload_micros for event in events if event.turn_index == turn_index)


def reload_details(events: Sequence[InvalidationEvent]) -> dict[int, str]:
    """Turn index -> user-facing detail, the shape :func:`lanes.classify_session` consumes."""
    return {event.turn_index: event.detail for event in events}


@dataclass(frozen=True)
class _Change:
    """One detected prefix change, before it is costed."""

    tier: str
    trigger: str
    detail: str
    source_ref: str


def _detect_changes(
    timeline: Timeline, turn_index: int, attachments: Sequence[AttachmentRecord]
) -> list[_Change]:
    changes: list[_Change] = []

    previous = timeline.turns[turn_index - 1]
    current = timeline.turns[turn_index]
    if previous.model != current.model:
        # A different model is a different cache entirely — nothing carries over, so this is
        # the most severe change there is.
        changes.append(
            _Change(
                tier="tools",
                trigger="model_switched",
                detail=f"model switched from {previous.model} to {current.model}",
                source_ref=f"{current.uuid}@line{current.line}",
            )
        )

    for attachment in attachments:
        change = _change_for_attachment(attachment)
        if change is not None:
            changes.append(change)
    return changes


def _change_for_attachment(attachment: AttachmentRecord) -> _Change | None:
    source_ref = f"{attachment.uuid}@line{attachment.line}"
    if attachment.attachment_type in _TOOL_TIER_ATTACHMENTS:
        detail = _describe_tool_change(attachment)
        if detail is None:
            return None
        return _Change(
            tier="tools", trigger="tool_set_changed", detail=detail, source_ref=source_ref
        )

    if attachment.attachment_type not in _SYSTEM_TIER_ATTACHMENTS:
        return None
    detail = _describe_instruction_change(attachment)
    if detail is None:
        return None
    return _Change(
        tier="system", trigger="instruction_changed", detail=detail, source_ref=source_ref
    )


def _describe_tool_change(attachment: AttachmentRecord) -> str | None:
    """Name the tools or MCP servers that came and went, as a user would name them."""
    added = _names(attachment.payload, "addedNames")
    removed = _names(attachment.payload, "removedNames")
    if not added and not removed:
        return None
    parts = [
        _describe_names(names, verb)
        for verb, names in (("added", added), ("removed", removed))
        if names
    ]
    return "; ".join(parts)


def _describe_names(names: Sequence[str], verb: str) -> str:
    """ "MCP server 'playwright' added (12 tools)" — the sentence a reader can act on.

    MCP tools are named ``mcp__<server>__<tool>``, so they roll up to the server the user
    actually added. That is the unit someone can remove again; an individual tool name is not.
    """
    servers: dict[str, int] = {}
    plain: list[str] = []
    for name in names:
        server = _mcp_server(name)
        if server is None:
            plain.append(name)
        else:
            servers[server] = servers.get(server, 0) + 1

    parts = [
        f"MCP server '{server}' {verb} ({count} tool{'s' if count != 1 else ''})"
        for server, count in sorted(servers.items())
    ]
    if plain:
        shown = ", ".join(sorted(plain)[:_MAX_NAMED])
        extra = len(plain) - _MAX_NAMED
        if extra > 0:
            shown = f"{shown} +{extra} more"
        parts.append(f"tool{'s' if len(plain) != 1 else ''} {verb}: {shown}")
    return "; ".join(parts)


def _mcp_server(tool_name: str) -> str | None:
    if not tool_name.startswith("mcp__"):
        return None
    remainder = tool_name[len("mcp__") :]
    server, separator, _ = remainder.partition("__")
    return server if separator and server else None


def _describe_instruction_change(attachment: AttachmentRecord) -> str | None:
    if attachment.attachment_type == "skill_listing":
        names = _names(attachment.payload, "names")
        shown = ", ".join(sorted(names)[:_MAX_NAMED]) if names else "unnamed"
        return f"skill listing changed ({len(names)} skills: {shown})"

    identity = attachment.identity
    if identity is None:
        return None
    # An edited source file is ordinary message growth. An edited instruction file re-renders
    # the system tier, which is what forces every later instruction file to be written again.
    if categorize(identity).category not in _INSTRUCTION_CATEGORIES:
        return None
    return f"instruction file '{identity}' changed"


def _build_event(
    timeline: Timeline,
    turn_index: int,
    changes: Sequence[_Change],
    write: TurnWrite | None,
    session_id: str,
) -> InvalidationEvent:
    primary = min(changes, key=lambda change: _TRIGGER_PRECEDENCE.index(change.trigger))
    # One event per turn: two changes arriving together cannot each be charged the same
    # re-write. The quieter one is named, not costed.
    detail = "; ".join(dict.fromkeys(change.detail for change in changes))

    reloaded_ids, reloaded_tokens = _reloaded(timeline, turn_index)
    micros, basis, confidence = _excess_write_cost(timeline, turn_index, write, reloaded_tokens)

    return InvalidationEvent(
        event_id=f"inval:{session_id}:{turn_index}",
        turn_index=turn_index,
        tier=primary.tier,
        trigger=primary.trigger,
        detail=detail,
        forced_reload_micros=micros,
        items_reloaded=len(reloaded_ids),
        basis=basis,
        confidence=confidence,
        source_refs=tuple(dict.fromkeys(change.source_ref for change in changes)),
    )


def _reloaded(timeline: Timeline, turn_index: int) -> tuple[list[str], int]:
    """Items that were already resident before this turn, and so had to be written again.

    Content arriving *this* turn was going to be written anyway; only what was already in the
    cache can have been forced back into it by the change.
    """
    injected = {injection.item_id for injection in timeline.injections_at(turn_index)}
    item_ids = [
        span.item_id
        for span in timeline.resident_at(turn_index)
        if span.first_turn < turn_index and span.item_id not in injected
    ]
    unique = list(dict.fromkeys(item_ids))
    return unique, sum(timeline.items[item_id].size_tokens for item_id in unique)


def _excess_write_cost(
    timeline: Timeline,
    turn_index: int,
    write: TurnWrite | None,
    reloaded_tokens: int,
) -> tuple[int, str, str]:
    """The part of this turn's write charge the invalidation explains — bounded twice.

    ``excess = write_tokens - newly_arrived_tokens`` is what arriving content leaves
    unexplained, but a turn-level join is loose in *both* directions (§5.3, median ratio
    3.31), so the excess is also capped at ``reloaded_tokens`` — the most that could possibly
    have been re-written. The smaller bound wins, the share is floored rather than rounded,
    and anything above it stays unattributed. Attributing less is the correct failure mode
    here: a confidently wrong "that MCP server cost you $40" is worse than no figure.
    """
    if write is None or write.write_tokens <= 0 or write.write_micros <= 0:
        # No write was charged, so the change forced no re-write we can observe. The event is
        # still reported — the reader wants to see it happened — with no cost claimed.
        return 0, "measured", "high"

    arrived = sum(injection.size_tokens for injection in timeline.injections_at(turn_index))
    unexplained = write.write_tokens - arrived
    excess_tokens = min(unexplained, reloaded_tokens)
    if excess_tokens <= 0:
        return 0, "measured", "medium"

    micros = write.write_micros * excess_tokens // write.write_tokens
    # `estimated`, never `measured`: the token split is a bound, not a reading. Confidence is
    # low for the same reason, and the unknown-TTL cap can only lower it further.
    return micros, "estimated", write.confidence_cap or "low"


def _attachments_by_turn(
    timeline: Timeline, attachments: Sequence[AttachmentRecord]
) -> Mapping[int, list[AttachmentRecord]]:
    """Group attachments by the turn that first *paid* for them.

    Same rule as the residency timeline: a record produced after turn N is sent in the request
    for turn N+1. Derived from the turns' own line numbers rather than from the timeline's
    injections, because an injection the sizer could not size is absent there — and a tool-set
    change we cannot size is still a tool-set change.
    """
    turn_lines = sorted((turn.line, index) for index, turn in enumerate(timeline.turns))
    grouped: dict[int, list[AttachmentRecord]] = {}
    for attachment in attachments:
        for line, index in turn_lines:
            if line > attachment.line:
                grouped.setdefault(index, []).append(attachment)
                break
    return grouped


def _names(payload: Mapping[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry]
