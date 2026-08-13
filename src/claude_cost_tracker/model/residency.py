"""Which items were in the conversation, when, and for how long.

This is where **carry cost** comes from, and carry is roughly half of all spend — measured at
~54% against ~22% for the initial load. An accounting that charges a file only for the moment
it was read explains under a quarter of the money and ranks the wrong files as expensive.

Three rules govern the timeline:

**Content becomes resident on the turn after it arrives.** A tool result produced after turn N
is sent in the request for turn N+1, so that is the first turn it is paid for.

**Carry stops when content leaves** (FR-004). Compaction is the one departure we can observe
exactly: the boundary record lists the messages that survived, so everything else prior to it
is evicted — no heuristic required (pass-2 §2.1).

**Departure is not always recorded.** Claude Code clears older tool outputs *before* it
compacts, and that leaves no marker anywhere in the transcript. Cost we can no longer tie to a
resident item must therefore surface in the unattributed remainder rather than being spread
across the items that happen to still be there (pass-2 §2.4, FR-013).
"""

import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from claude_cost_tracker.config.categories import categorize
from claude_cost_tracker.ingest.records import (
    AttachmentRecord,
    CompactionRecord,
    ToolResultRecord,
    TurnRecord,
)
from claude_cost_tracker.ingest.skills import parse_listing
from claude_cost_tracker.ingest.tokens import TokenQuantity

# Why an item entered the conversation. `compact_reinjection` matters on its own: CLAUDE.md
# reappears after a compaction as a *new* write from disk, not a continuing span, so its cost
# basis resets rather than accumulating (pass-2 §2.2).
INJECTION_CAUSES: tuple[str, ...] = (
    "tool_result",
    "attachment",
    "at_mention",
    "skill_listing",
    "deferred_tools_delta",
    "session_start",
    "compact_reinjection",
)

END_REASONS: tuple[str, ...] = ("evicted", "invalidated", "session_end", "unknown")

# Attachment types that name a file, versus those that are resident instruction content.
_FILE_ATTACHMENTS: frozenset[str] = frozenset(
    {"file", "edited_text_file", "compact_file_reference"}
)
_INSTRUCTION_ATTACHMENTS: dict[str, str] = {
    "skill_listing": "skill",
    "deferred_tools_delta": "tool_schema",
    "agent_listing_delta": "tool_schema",
}


@dataclass(frozen=True)
class ItemPart:
    """One named piece of a composite item, weighted by its share of the whole.

    ``weight`` is characters of the listing text — measured, not modelled. ``origin`` says
    whether the reader can do anything about it: a skill supplied by an installed plugin is
    named ``plugin:skill`` by Claude Code and is not something editing this repo can change.
    """

    name: str
    weight: int
    origin: str
    measured: bool = True


@dataclass(frozen=True)
class ContextItem:
    """Anything occupying space in the conversation, and therefore incurring cost."""

    item_id: str
    kind: str
    identity: str
    category: str
    size_tokens: int
    basis: str
    confidence: str
    project_path: str | None = None
    # What this item is made of, where it is a listing of many things rather than one thing.
    # The skill catalogue is injected and cached as ONE block, so it stays one item — its
    # cacheability is a property of the whole block, and splitting it into forty small items
    # would push each below the minimum cacheable size and price them in a different lane
    # (measured: the same session moved from $1.14 to $0.32 on nothing but the split). So the
    # parts live here, as a breakdown of the item's cost, and the pricing is untouched.
    parts: tuple[ItemPart, ...] = ()


@dataclass(frozen=True)
class Injection:
    """One event placing an item into the conversation. Origin of direct cost."""

    injection_id: str
    item_id: str
    turn_index: int
    cause: str
    size_tokens: int
    source_ref: str


@dataclass
class ResidencySpan:
    """The interval an item remained available. Origin of carry cost."""

    span_id: str
    item_id: str
    first_turn: int
    last_turn: int | None = None
    end_reason: str | None = None

    def covers(self, turn_index: int) -> bool:
        if turn_index < self.first_turn:
            return False
        return self.last_turn is None or turn_index <= self.last_turn

    def turns_resident(self, final_turn_index: int) -> int:
        end = final_turn_index if self.last_turn is None else self.last_turn
        return max(0, end - self.first_turn + 1)


@dataclass
class Timeline:
    """The per-turn resident set for a session, plus the events that shaped it."""

    turns: list[TurnRecord] = field(default_factory=list)
    items: dict[str, ContextItem] = field(default_factory=dict)
    injections: list[Injection] = field(default_factory=list)
    spans: list[ResidencySpan] = field(default_factory=list)
    compaction_turns: list[int] = field(default_factory=list)
    # Tokens a compaction dropped beyond what its own boundary explains: content cleared
    # before compaction, which leaves no marker. Displayed as a named residual, never folded
    # into carry (pass-2 §2.4).
    unexplained_dropped_tokens: int = 0

    @property
    def final_turn_index(self) -> int:
        return len(self.turns) - 1

    def resident_at(self, turn_index: int) -> list[ResidencySpan]:
        """Spans covering this turn, in a stable order so figures are reproducible."""
        return [span for span in self.spans if span.covers(turn_index)]

    def weights_at(self, turn_index: int) -> tuple[list[str], list[int]]:
        """Item ids and their token weights for a turn — the input to a carry split."""
        spans = self.resident_at(turn_index)
        return (
            [span.item_id for span in spans],
            [self.items[span.item_id].size_tokens for span in spans],
        )

    def injections_at(self, turn_index: int) -> list[Injection]:
        return [inj for inj in self.injections if inj.turn_index == turn_index]

    def spans_for(self, item_id: str) -> list[ResidencySpan]:
        return [span for span in self.spans if span.item_id == item_id]

    def load_count(self, item_id: str) -> int:
        """How many times this item was loaded — the "read 40 times" half of US2."""
        return sum(1 for inj in self.injections if inj.item_id == item_id)

    def turns_resident(self, item_id: str) -> int:
        """How many turns this item was carried — the "resident 58 turns" half of US2."""
        return sum(span.turns_resident(self.final_turn_index) for span in self.spans_for(item_id))


Sizer = Callable[[ToolResultRecord | AttachmentRecord], TokenQuantity]


def build_timeline(
    turns: Sequence[TurnRecord],
    tool_results: Sequence[ToolResultRecord],
    attachments: Sequence[AttachmentRecord],
    compactions: Sequence[CompactionRecord],
    *,
    sizer: Sizer,
    project_path: str | None = None,
) -> Timeline:
    """Reconstruct what was resident at every turn.

    The sizer is injected rather than imported so this stage can be tested over fixture data
    with a known, hand-checkable size for every item — the arithmetic is what is under test
    here, not the token measurement.
    """
    timeline = Timeline(turns=list(turns))
    if not timeline.turns:
        return timeline

    # Records are ordered by their position in the file. A tool result or attachment belongs
    # to the first turn that comes *after* it, because that is the request it is sent in.
    turn_line_index = {turn.line: index for index, turn in enumerate(turns)}
    turn_lines = sorted(turn_line_index)

    events: list[tuple[int, str, object]] = []
    events.extend((record.line, "injection", record) for record in tool_results)
    events.extend((record.line, "injection", record) for record in attachments)
    events.extend((record.line, "compaction", record) for record in compactions)
    events.sort(key=lambda event: event[0])

    open_spans: dict[str, ResidencySpan] = {}

    for line, kind, record in events:
        if kind == "compaction":
            assert isinstance(record, CompactionRecord)
            _apply_compaction(timeline, open_spans, record, line, turn_lines, turn_line_index)
            continue

        assert isinstance(record, (ToolResultRecord, AttachmentRecord))
        turn_index = _next_turn_index(line, turn_lines, turn_line_index)
        if turn_index is None:
            # Injected after the final turn: it was never paid for, so it is not an item.
            continue
        _apply_injection(timeline, open_spans, record, turn_index, sizer, project_path)

    for span in open_spans.values():
        span.last_turn = None
        span.end_reason = "session_end"
    return timeline


def _apply_injection(
    timeline: Timeline,
    open_spans: dict[str, ResidencySpan],
    record: ToolResultRecord | AttachmentRecord,
    turn_index: int,
    sizer: Sizer,
    project_path: str | None,
) -> None:
    identity, kind, cause = _identify(record)
    if identity is None:
        return
    identity = absolute_identity(identity, kind, getattr(record, "cwd", None), project_path)
    sized = sizer(record)
    if sized.tokens is None or sized.tokens <= 0:
        # A withheld size (an image whose header we could not read) is not an item with a
        # weight of zero — it is a gap. Attributing zero would understate it silently, so it
        # is left out and its cost lands in the unattributed remainder instead (FR-019).
        return

    item_id = _item_id(identity, kind, project_path)
    existing = timeline.items.get(item_id)
    if existing is None or sized.tokens > existing.size_tokens:
        # Re-reading a file that grew: the larger observed size is the one that was carried.
        timeline.items[item_id] = ContextItem(
            item_id=item_id,
            kind=kind,
            identity=identity,
            category=categorize(identity, kind=kind).category,
            size_tokens=sized.tokens,
            basis=sized.basis,
            confidence=sized.confidence,
            project_path=project_path,
            parts=_parts_of(record),
        )

    timeline.injections.append(
        Injection(
            injection_id=f"{record.uuid}:{item_id}",
            item_id=item_id,
            turn_index=turn_index,
            cause=cause,
            size_tokens=sized.tokens,
            source_ref=f"{record.uuid}@line{record.line}",
        )
    )

    # A file read, modified, and read again produces distinct injections. Its residency is
    # continuous, so an already-open span is extended rather than duplicated; the reload shows
    # up as a second injection (direct cost), which is what distinguishes "read 40 times" from
    # "read once and carried".
    if item_id not in open_spans:
        span = ResidencySpan(
            span_id=f"span:{item_id}:{turn_index}",
            item_id=item_id,
            first_turn=turn_index,
        )
        open_spans[item_id] = span
        timeline.spans.append(span)


def _apply_compaction(
    timeline: Timeline,
    open_spans: dict[str, ResidencySpan],
    record: CompactionRecord,
    line: int,
    turn_lines: Sequence[int],
    turn_line_index: dict[int, int],
) -> None:
    """Close the spans a compaction evicted, using the boundary's own survivor list."""
    boundary_turn = _previous_turn_index(line, turn_lines, turn_line_index)
    if boundary_turn is None:
        boundary_turn = 0
    timeline.compaction_turns.append(boundary_turn)

    # Everything before the boundary that is not named as preserved was evicted. An empty
    # survivor list means everything went.
    surviving_uuids = record.preserved_uuids
    for item_id, span in list(open_spans.items()):
        injections = [inj for inj in timeline.injections if inj.item_id == item_id]
        survived = any(inj.source_ref.split("@", 1)[0] in surviving_uuids for inj in injections)
        if survived:
            continue
        span.last_turn = boundary_turn
        span.end_reason = "evicted"
        del open_spans[item_id]

    # `cumulativeDroppedTokens` runs across the whole session, so only the part this boundary
    # does not explain is a residual. Tracked, displayed, never absorbed into carry.
    unexplained = record.cumulative_dropped_tokens - record.dropped_tokens
    if unexplained > 0:
        timeline.unexplained_dropped_tokens = max(timeline.unexplained_dropped_tokens, unexplained)


def _parts_of(record: ToolResultRecord | AttachmentRecord) -> tuple[ItemPart, ...]:
    """The individual skills a catalogue lists, so "Skills: $67" can be broken down.

    A listing is one cached block and stays one item; this only records what is inside it, so a
    surface can divide the item's cost by each entry's share of the listing text. Anything that
    is not a listing has no parts, which is the normal case.
    """
    payload = getattr(record, "payload", None)
    if not isinstance(payload, dict):
        return ()
    content = payload.get("content")
    listed = parse_listing(content if isinstance(content, str) else "", payload.get("names"))
    return tuple(
        ItemPart(
            name=skill.name,
            weight=skill.characters,
            origin=skill.origin,
            measured=skill.measured,
        )
        for skill in listed
    )


def _identify(record: ToolResultRecord | AttachmentRecord) -> tuple[str | None, str, str]:
    """The item identity, kind, and injection cause for a record."""
    if isinstance(record, ToolResultRecord):
        return _tool_result_identity(record), "file", "tool_result"

    attachment_type = record.attachment_type
    if attachment_type in _FILE_ATTACHMENTS:
        cause = "at_mention" if attachment_type == "file" else "attachment"
        return record.identity, "file", cause
    if attachment_type in _INSTRUCTION_ATTACHMENTS:
        kind = _INSTRUCTION_ATTACHMENTS[attachment_type]
        return attachment_type, kind, attachment_type
    return None, "conversation", "attachment"


def _tool_result_identity(record: ToolResultRecord) -> str | None:
    """The file a tool result returned, where it names one.

    A `Read` payload carries the path directly. A Bash result does not name a file at all —
    its cost belongs to the conversation, not to any item, so it is deliberately not given a
    synthetic identity.
    """
    payload = record.payload
    if isinstance(payload, dict):
        file_block = payload.get("file")
        if isinstance(file_block, dict):
            path = file_block.get("filePath")
            if isinstance(path, str) and path:
                return path
        for key in ("filePath", "file_path"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def absolute_identity(identity: str, kind: str, cwd: str | None, project_path: str | None) -> str:
    """Resolve a relative file path against the directory the turn ran in.

    A tool result names its file however the caller wrote it, so the *same file* arrives as
    ``tests/unit/test_money.py`` in one turn and
    ``/Users/me/projects/claude-cost-tracker/tests/unit/test_money.py`` in another. Left alone those are two
    identities, so one file becomes two rows holding half its cost each, and neither appears
    under its folder in the tree — which is how a project folder ends up understated.

    ``cwd`` is recorded on every transcript record and is the *observed* base, not an assumed
    one: it changes within a session, so resolving against the project path instead would be a
    guess that is sometimes wrong. The project path is the fallback, and where neither is known
    the path is left exactly as it was recorded rather than being rooted at "/" — inventing a
    location is worse than admitting there is none (Principle X).

    Only files are resolved. ``skill_listing`` and the tool-schema deltas are not paths.
    """
    if kind != "file" or _is_absolute(identity):
        return identity
    base = cwd or project_path
    if not base:
        return identity
    # `PurePosixPath` cannot fold "..", and a transcript is full of them, so this goes through
    # `posixpath.normpath` — lexical, which is right here: the file may no longer exist, and a
    # figure must not depend on what happens to be on this disk today.
    return posixpath.normpath(posixpath.join(base.replace("\\", "/"), identity.replace("\\", "/")))


def _is_absolute(path: str) -> bool:
    """POSIX roots, Windows drive letters, and UNC shares all count as already-located."""
    return path.startswith(("/", "\\")) or (len(path) > 2 and path[1] == ":")


def _item_id(identity: str, kind: str, project_path: str | None) -> str:
    """A stable id, scoped by project **only where the identity is ambiguous without it**.

    The scope exists so that two projects' ``src/app.py`` stay distinct. An absolute path is
    already unique on one machine, so scoping it disambiguates nothing — and it actively harms:
    a file read from two sessions that recorded different project metadata (one resolved, one
    not) became two rows, each carrying part of that file's cost. Measured on a real corpus:
    19 identities split that way, covering 16% of the spend. The totals still reconciled, which
    is what made it invisible — every one of those files was simply ranked at a fraction of what
    it actually cost (Principle X).

    So the scope is applied to exactly the case it was introduced for: an identity that could
    not be resolved to an absolute path, and therefore means different files in different
    projects.
    """
    if _is_absolute(identity):
        return f"{kind}:{identity}"
    scope = project_path or "-"
    return f"{kind}:{scope}:{identity}"


def _next_turn_index(
    line: int, turn_lines: Sequence[int], turn_line_index: dict[int, int]
) -> int | None:
    for turn_line in turn_lines:
        if turn_line > line:
            return turn_line_index[turn_line]
    return None


def _previous_turn_index(
    line: int, turn_lines: Sequence[int], turn_line_index: dict[int, int]
) -> int | None:
    previous: int | None = None
    for turn_line in turn_lines:
        if turn_line >= line:
            break
        previous = turn_line_index[turn_line]
    return previous
