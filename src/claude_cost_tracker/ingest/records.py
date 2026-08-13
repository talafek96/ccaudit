"""Transcript record types and parsing — facts in, never conclusions.

This is the bottom of the ingest layer: it turns a Claude Code JSONL transcript into typed
records, and does nothing else. Every number it produces is read from the file; nothing here
derives, estimates, or explains. (Explaining is `model/`'s job, and it may only explain what
was observed — docs/cost-model.md §5.1.)

Three things about the format drive the whole design, all measured against a real 23-session
corpus (docs/research/prior-art-pass-2.md §5):

**Most records are not conversation.** Roughly 60% are UI state — titles, modes, queue
operations, file-history snapshots. A parser that treats an unrecognised ``type`` as an error
rejects the majority of the file. So we distinguish *recognised and irrelevant* (skipped, and
counted) from *malformed* (counted and surfaced as a diagnostic, never silently dropped —
FR-027).

**`input_tokens` is not the prompt size.** It is the uncached remainder. Conversation size is
``input + cache_creation + cache_read``, and :class:`Usage` exposes it that way so no call site
can make that mistake (FR-083).

**Content arrives by more routes than tool results.** ``attachment`` records carry
``@``-mention injections, the skill listing, and tool-schema deltas — the residency changes a
tool-call-only walk misses entirely (FR-022, pass-2 §5.5).
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Record types that are conversation or residency events — everything we actually parse.
CONVERSATION_TYPES: frozenset[str] = frozenset({"assistant", "user", "attachment", "system"})

# Recognised UI-state and bookkeeping records. Skipped deliberately and counted, so "we ignored
# 6,000 records" is a visible fact rather than an assumption (FR-027).
IGNORED_TYPES: frozenset[str] = frozenset(
    {
        "last-prompt",
        "mode",
        "permission-mode",
        "ai-title",
        "custom-title",
        "agent-name",
        "file-history-snapshot",
        "file-history-delta",
        "bridge-session",
        "queue-operation",
        "summary",
        "diff",
    }
)

# Claude Code writes this in place of a model id on assistant messages it generated *locally* —
# "Not logged in · Please run /login", "API Error: Connection closed mid-response". They are not
# API calls, they carry all-zero usage, and they were never billed. Observed on 9 of 32 sessions
# in the local corpus, so a parser that treats an unpriceable model as fatal rejects a quarter of
# real transcripts.
SYNTHETIC_MODEL = "<synthetic>"

# Attachment types that place content into the conversation. Others (date changes, permission
# echoes) occupy no meaningful space and are counted as ignored rather than treated as items.
CONTENT_ATTACHMENT_TYPES: frozenset[str] = frozenset(
    {
        "file",
        "edited_text_file",
        "directory",
        "skill_listing",
        "deferred_tools_delta",
        "agent_listing_delta",
        "task_reminder",
        "compact_file_reference",
        "total_tokens_reminder",
        "queued_command",
    }
)


class TranscriptFormatError(ValueError):
    """A record was recognised but could not be parsed. Carries the file, line, and reason."""


@dataclass(frozen=True)
class Usage:
    """The four billed token classes for one turn, exactly as recorded.

    Never adjusted. These are the observed facts that every attribution must reconcile back
    to (docs/cost-model.md §5.1).
    """

    input_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    # Written to cache by a record that gave a flat total with no window breakdown. Kept apart
    # from the two known windows rather than folded into either: the write multiplier differs
    # by 60% between them, so this is the one part of a write whose price is genuinely a guess,
    # and it is the only part whose confidence should be capped (FR-080).
    cache_creation_unknown_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    @property
    def cache_creation_tokens(self) -> int:
        """Total written to cache this turn, across both reuse windows and the unattributed."""
        return (
            self.cache_creation_5m_tokens
            + self.cache_creation_1h_tokens
            + self.cache_creation_unknown_tokens
        )

    @property
    def prompt_tokens(self) -> int:
        """Total conversation size for this turn — all three input measures (FR-083).

        A session showing 4K ``input_tokens`` after hours of work is not a small session; the
        rest was served from cache. Reporting ``input_tokens`` as the prompt size is the trap
        this property exists to close.
        """
        return self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens

    @property
    def ttl(self) -> str | None:
        """Which single reuse window this turn's write used, where there is only one.

        Reported, not priced against: a turn that writes into *both* windows is not a turn with
        an unknown TTL, it is a turn with two known ones, and pricing splits it (`price_turn`).
        ``None`` here means "no single answer", which is a fact about this label rather than a
        gap in what was recorded.
        """
        has_5m = self.cache_creation_5m_tokens > 0
        has_1h = self.cache_creation_1h_tokens > 0
        if has_5m and not has_1h:
            return "5m"
        if has_1h and not has_5m:
            return "1h"
        return None


@dataclass(frozen=True)
class TurnRecord:
    """One assistant exchange — the unit at which cost is observed.

    ``message_id`` and ``request_id`` together are the dedup key: the same exchange appears in
    more than one file across resume, fork, and compaction, and counting it twice inflates
    every downstream figure (FR-021).
    """

    uuid: str
    line: int
    message_id: str | None
    request_id: str | None
    model: str
    usage: Usage
    parent_uuid: str | None = None
    logical_parent_uuid: str | None = None
    session_id: str | None = None
    timestamp: str | None = None
    version: str | None = None
    is_sidechain: bool = False
    agent_id: str | None = None
    attribution_skill: str | None = None
    attribution_agent: str | None = None
    attribution_plugin: str | None = None
    tool_use_ids: tuple[str, ...] = ()

    @property
    def dedup_key(self) -> tuple[str, str]:
        """The identity used to count each exchange exactly once.

        Falls back to the record ``uuid`` when the API identifiers are absent, so a record
        without them is still counted once rather than merged with every other such record.
        """
        return (self.message_id or self.uuid, self.request_id or self.uuid)


@dataclass(frozen=True)
class ToolResultRecord:
    """A tool result returning content into the conversation. Origin of direct cost."""

    uuid: str
    line: int
    tool_use_id: str | None
    tool_name: str | None
    parent_uuid: str | None = None
    session_id: str | None = None
    timestamp: str | None = None
    is_sidechain: bool = False
    is_meta: bool = False
    # The raw `toolUseResult` payload. Its shape varies by tool and it is not part of the API
    # message, so sizing it is `tokens.py`'s job, not this parser's.
    payload: Any = None
    # The tool_result block's own content, from inside the API message. Images live *here*, not
    # in `toolUseResult` — a screenshot's `toolUseResult` is often just `{"isImage": true}`.
    # Without this the pixels never reach the sizer and every embedded image is withheld, which
    # silently drops the single largest contributor to tool-result volume.
    content: Any = None
    text_length: int = 0
    # The working directory the turn ran in. A tool result can name a file *relatively*
    # ("tests/unit/test_money.py"), and without this there is nothing to resolve it against —
    # so the same file read once by relative path and once absolutely becomes two items, each
    # holding half its cost.
    cwd: str | None = None


@dataclass(frozen=True)
class AttachmentRecord:
    """A content-bearing injection that is not a tool result.

    ``@``-mentions, the skill listing, tool-schema deltas, and files carried across a
    compaction all arrive this way. A residency model that walks only tool calls misses every
    one of them (pass-2 §5.5).
    """

    uuid: str
    line: int
    attachment_type: str
    parent_uuid: str | None = None
    session_id: str | None = None
    timestamp: str | None = None
    is_sidechain: bool = False
    identity: str | None = None
    text_length: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    # As on a tool result: an @-mention can name a file relatively too.
    cwd: str | None = None


@dataclass(frozen=True)
class CompactionRecord:
    """A compaction boundary — the exact record of what left the conversation.

    ``preserved_uuids`` is authoritative: everything before the boundary that is not in it was
    evicted. This is what makes residency resettable exactly rather than heuristically
    (FR-025, pass-2 §2.1).

    ``cumulative_dropped_tokens`` is *cumulative across the session*, not per-boundary. The
    portion it exceeds the compaction-explained drop by is Claude Code clearing older tool
    outputs before compacting, which leaves no other marker — a named residual, never absorbed
    silently into carry cost (pass-2 §2.4).
    """

    uuid: str
    line: int
    trigger: str | None
    pre_tokens: int
    post_tokens: int
    cumulative_dropped_tokens: int
    preserved_uuids: frozenset[str]
    parent_uuid: str | None = None
    logical_parent_uuid: str | None = None
    session_id: str | None = None
    timestamp: str | None = None

    @property
    def dropped_tokens(self) -> int:
        """Tokens this boundary removed, from the record's own before/after counts."""
        return max(0, self.pre_tokens - self.post_tokens)


@dataclass
class IngestDiagnostic:
    """A record we could not use, counted and named rather than skipped in silence (FR-027)."""

    kind: str
    count: int = 0
    samples: list[str] = field(default_factory=list)

    def record(self, sample: str, max_samples: int = 5) -> None:
        self.count += 1
        if len(self.samples) < max_samples:
            self.samples.append(sample)


@dataclass
class ParsedTranscript:
    """Everything one transcript file yielded, plus what it could not."""

    path: Path
    session_id: str | None = None
    producing_versions: set[str] = field(default_factory=set)
    turns: list[TurnRecord] = field(default_factory=list)
    tool_results: list[ToolResultRecord] = field(default_factory=list)
    attachments: list[AttachmentRecord] = field(default_factory=list)
    compactions: list[CompactionRecord] = field(default_factory=list)
    diagnostics: dict[str, IngestDiagnostic] = field(default_factory=dict)
    record_count: int = 0
    ignored_count: int = 0

    def note(self, kind: str, sample: str) -> None:
        """Record a diagnostic. Every unparseable record lands here, none are dropped."""
        self.diagnostics.setdefault(kind, IngestDiagnostic(kind=kind)).record(sample)

    @property
    def unparseable_count(self) -> int:
        return sum(d.count for d in self.diagnostics.values())

    @property
    def spans_versions(self) -> bool:
        """More than one producing version — comparisons across it must say so (FR-028)."""
        return len(self.producing_versions) > 1


def parse_transcript(path: Path) -> ParsedTranscript:
    """Parse one JSONL transcript. Read-only; never writes to or moves the source (FR-020)."""
    parsed = ParsedTranscript(path=path)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            _consume_line(parsed, line, line_number)
    return parsed


def iter_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield ``(line number, record)`` for well-formed JSON objects. Used by the fingerprinter."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield line_number, record


def _consume_line(parsed: ParsedTranscript, line: str, line_number: int) -> None:
    stripped = line.strip()
    if not stripped:
        return
    parsed.record_count += 1

    try:
        record = json.loads(stripped)
    except json.JSONDecodeError as exc:
        parsed.note("unparseable_json", f"{parsed.path.name}:{line_number}: {exc.msg}")
        return
    if not isinstance(record, dict):
        parsed.note("not_an_object", f"{parsed.path.name}:{line_number}: {type(record).__name__}")
        return

    # Session id and version are carried forward: `version` is absent on ~27% of records, so a
    # per-row read would under-report which Claude Code versions produced a session (FR-028).
    session_id = _first_str(record, "sessionId", "session_id")
    if session_id and parsed.session_id is None:
        parsed.session_id = session_id
    version = _as_str(record.get("version"))
    if version:
        parsed.producing_versions.add(version)

    record_type = _as_str(record.get("type"))
    if record_type is None:
        parsed.note("missing_type", f"{parsed.path.name}:{line_number}")
        return
    if record_type in IGNORED_TYPES or record_type not in CONVERSATION_TYPES:
        parsed.ignored_count += 1
        return

    try:
        _dispatch(parsed, record, record_type, line_number)
    except TranscriptFormatError as exc:
        parsed.note("malformed_record", str(exc))


def _dispatch(
    parsed: ParsedTranscript, record: dict[str, Any], record_type: str, line_number: int
) -> None:
    if record_type == "assistant":
        turn = _parse_assistant(record, line_number, parsed.path)
        if turn is not None:
            parsed.turns.append(turn)
        else:
            parsed.ignored_count += 1
    elif record_type == "user":
        result = _parse_user(record, line_number)
        if result is not None:
            parsed.tool_results.append(result)
        else:
            parsed.ignored_count += 1
    elif record_type == "attachment":
        attachment = _parse_attachment(record, line_number)
        if attachment is not None:
            parsed.attachments.append(attachment)
        else:
            parsed.ignored_count += 1
    elif record_type == "system":
        compaction = _parse_system(record, line_number)
        if compaction is not None:
            parsed.compactions.append(compaction)
        else:
            parsed.ignored_count += 1


def _parse_assistant(record: dict[str, Any], line: int, path: Path) -> TurnRecord | None:
    """Parse an assistant record. Returns ``None`` when it carries no billable usage."""
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage_raw = message.get("usage")
    if not isinstance(usage_raw, dict):
        # An assistant record without usage is a continuation fragment, not a charge.
        return None

    model = _as_str(message.get("model")) or _as_str(record.get("model"))
    if not model:
        raise TranscriptFormatError(
            f"{path.name}:{line}: assistant record has usage but no model, so its charges "
            f"cannot be priced; uuid={record.get('uuid')!r}"
        )

    usage = _parse_usage(usage_raw, path, line)

    if model == SYNTHETIC_MODEL:
        # Locally generated, never sent to the API, never billed. Not a turn.
        if usage.prompt_tokens or usage.output_tokens:
            # If one ever does carry usage, it was a real charge and dropping it would
            # understate the session. Surface it rather than quietly losing the money.
            raise TranscriptFormatError(
                f"{path.name}:{line}: a {SYNTHETIC_MODEL} record carries non-zero usage "
                f"({usage.prompt_tokens} prompt, {usage.output_tokens} output). These are "
                f"assumed to be locally generated and unbilled; that assumption no longer "
                f"holds and the charge would be dropped silently."
            )
        return None

    return TurnRecord(
        uuid=_as_str(record.get("uuid")) or f"{path.name}:{line}",
        line=line,
        message_id=_as_str(message.get("id")) or _as_str(record.get("messageId")),
        request_id=_as_str(record.get("requestId")),
        model=model,
        usage=usage,
        parent_uuid=_as_str(record.get("parentUuid")),
        logical_parent_uuid=_as_str(record.get("logicalParentUuid")),
        session_id=_first_str(record, "sessionId", "session_id"),
        timestamp=_as_str(record.get("timestamp")),
        version=_as_str(record.get("version")),
        is_sidechain=bool(record.get("isSidechain")),
        agent_id=_as_str(record.get("agentId")),
        attribution_skill=_as_str(record.get("attributionSkill")),
        attribution_agent=_as_str(record.get("attributionAgent")),
        attribution_plugin=_as_str(record.get("attributionPlugin")),
        tool_use_ids=_tool_use_ids(message.get("content")),
    )


def _parse_usage(raw: dict[str, Any], path: Path, line: int) -> Usage:
    """Read the four billed token classes.

    ``usage.iterations`` is deliberately ignored: measured across 3,884 assistant messages,
    ``sum(iterations[].output_tokens) == usage.output_tokens`` exactly. The top level is
    already rolled up, and summing both double-counts (pass-2 §5.4). The identity is asserted
    rather than trusted, so a future multi-iteration shape surfaces as a diagnostic instead of
    a silent under-count.
    """
    creation = raw.get("cache_creation")
    if isinstance(creation, dict):
        five_minute = _as_int(creation.get("ephemeral_5m_input_tokens"))
        one_hour = _as_int(creation.get("ephemeral_1h_input_tokens"))
    else:
        # Older records carry a flat total with no window breakdown. Attribute it to neither
        # window: the TTL is then unknown, and pricing caps the figure's confidence (FR-080).
        five_minute = 0
        one_hour = 0
    total_creation = _as_int(raw.get("cache_creation_input_tokens"))
    # Anything the breakdown does not account for is kept as *unknown* rather than assigned to
    # a window. Filing it under 5m — which is what this used to do — prices a 1h write 60% too
    # low and reports it at full confidence, which is a confidently wrong figure.
    unknown = max(0, total_creation - five_minute - one_hour)

    usage = Usage(
        input_tokens=_as_int(raw.get("input_tokens")),
        cache_creation_5m_tokens=five_minute,
        cache_creation_1h_tokens=one_hour,
        cache_creation_unknown_tokens=unknown,
        cache_read_tokens=_as_int(raw.get("cache_read_input_tokens")),
        output_tokens=_as_int(raw.get("output_tokens")),
    )
    _assert_iterations_roll_up(raw, usage, path, line)
    return usage


def _assert_iterations_roll_up(raw: dict[str, Any], usage: Usage, path: Path, line: int) -> None:
    iterations = raw.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        return
    summed = sum(
        _as_int(entry.get("output_tokens")) for entry in iterations if isinstance(entry, dict)
    )
    if summed and summed != usage.output_tokens:
        raise TranscriptFormatError(
            f"{path.name}:{line}: usage.iterations output_tokens sum to {summed} but "
            f"usage.output_tokens is {usage.output_tokens}. The top level is assumed to be "
            f"already rolled up; this record breaks that assumption and would be miscounted."
        )


def _parse_user(record: dict[str, Any], line: int) -> ToolResultRecord | None:
    """Parse a user record, keeping only the ones returning content into the conversation."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    tool_use_id, text_length, result_content = _tool_result_shape(content)
    payload = record.get("toolUseResult")
    if tool_use_id is None and payload is None:
        # Real user text, a compaction summary, or an isMeta sidecar. Not an injection with an
        # identity we can attribute; its cost lands in conversation overhead.
        return None

    return ToolResultRecord(
        uuid=_as_str(record.get("uuid")) or f"line:{line}",
        line=line,
        tool_use_id=tool_use_id or _as_str(record.get("sourceToolUseID")),
        tool_name=_as_str(record.get("toolName")),
        parent_uuid=_as_str(record.get("parentUuid")),
        session_id=_first_str(record, "sessionId", "session_id"),
        timestamp=_as_str(record.get("timestamp")),
        is_sidechain=bool(record.get("isSidechain")),
        is_meta=bool(record.get("isMeta")),
        payload=payload,
        content=result_content,
        text_length=text_length,
        cwd=_as_str(record.get("cwd")),
    )


def _parse_attachment(record: dict[str, Any], line: int) -> AttachmentRecord | None:
    attachment = record.get("attachment")
    if not isinstance(attachment, dict):
        return None
    attachment_type = _as_str(attachment.get("type"))
    if attachment_type is None or attachment_type not in CONTENT_ATTACHMENT_TYPES:
        return None

    return AttachmentRecord(
        uuid=_as_str(record.get("uuid")) or f"line:{line}",
        line=line,
        attachment_type=attachment_type,
        parent_uuid=_as_str(record.get("parentUuid")),
        session_id=_first_str(record, "sessionId", "session_id"),
        timestamp=_as_str(record.get("timestamp")),
        is_sidechain=bool(record.get("isSidechain")),
        identity=_attachment_identity(attachment),
        text_length=_attachment_text_length(attachment),
        payload=attachment,
        cwd=_as_str(record.get("cwd")),
    )


def _parse_system(record: dict[str, Any], line: int) -> CompactionRecord | None:
    if _as_str(record.get("subtype")) != "compact_boundary":
        return None
    metadata = record.get("compactMetadata")
    if not isinstance(metadata, dict):
        raise TranscriptFormatError(
            f"line {line}: compact_boundary record has no compactMetadata, so what survived "
            f"the compaction cannot be determined; uuid={record.get('uuid')!r}"
        )

    preserved: set[str] = set()
    messages = metadata.get("preservedMessages")
    if isinstance(messages, dict):
        for key in ("allUuids", "uuids"):
            values = messages.get(key)
            if isinstance(values, list):
                preserved.update(v for v in values if isinstance(v, str))

    return CompactionRecord(
        uuid=_as_str(record.get("uuid")) or f"line:{line}",
        line=line,
        trigger=_as_str(metadata.get("trigger")),
        pre_tokens=_as_int(metadata.get("preTokens")),
        post_tokens=_as_int(metadata.get("postTokens")),
        cumulative_dropped_tokens=_as_int(metadata.get("cumulativeDroppedTokens")),
        preserved_uuids=frozenset(preserved),
        parent_uuid=_as_str(record.get("parentUuid")),
        # A compaction boundary carries parentUuid: null and logicalParentUuid instead. A DAG
        # walk that follows only parentUuid splits the session into disconnected components at
        # every compaction (pass-2 §2.1).
        logical_parent_uuid=_as_str(record.get("logicalParentUuid")),
        session_id=_first_str(record, "sessionId", "session_id"),
        timestamp=_as_str(record.get("timestamp")),
    )


def _tool_use_ids(content: Any) -> tuple[str, ...]:
    if not isinstance(content, list):
        return ()
    ids = [
        block["id"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use" and _as_str(block.get("id"))
    ]
    return tuple(ids)


def _tool_result_shape(content: Any) -> tuple[str | None, int, Any]:
    """The tool_result block's id, its rendered text length, and its raw content.

    The raw content is carried through because that is where image payloads live; sizing it is
    `tokens.py`'s job, but a parser that discards it makes that job impossible.
    """
    if not isinstance(content, list):
        return None, 0, None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        inner = block.get("content")
        return _as_str(block.get("tool_use_id")), _content_text_length(inner), inner
    return None, 0, None


def _content_text_length(content: Any) -> int:
    """Character length of a content payload.

    Characters, deliberately — not an estimated token count. Turning this into tokens is
    `tokens.py`'s job, and for images the conversion is by pixel dimensions, never by
    character count (research §6).

    Content nests differently per source: a tool result puts text at the top level, while an
    ``@``-mention wraps it as ``content.file.content``. Recursing through the content-bearing
    keys handles both without a shape check per attachment type.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_content_text_length(block) for block in content)
    if isinstance(content, dict):
        return sum(
            _content_text_length(content[key])
            for key in ("text", "content", "file", "snippet")
            if key in content
        )
    return 0


def _attachment_identity(attachment: dict[str, Any]) -> str | None:
    for key in ("displayPath", "filename", "path", "agentType"):
        value = _as_str(attachment.get(key))
        if value:
            return value
    return None


def _attachment_text_length(attachment: dict[str, Any]) -> int:
    """Characters of content this attachment injected.

    Schema and agent deltas carry their size as *line counts* of added and removed tool
    descriptions rather than as text, so they measure zero here and are sized from those
    counts in tokens.py — a zero from this function is not a claim that nothing was injected.
    """
    return sum(
        _content_text_length(attachment[key])
        for key in ("text", "content", "snippet", "prompt")
        if key in attachment
    )


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_str(record: dict[str, Any], *keys: str) -> str | None:
    """First present string among ``keys``.

    ``sessionId`` and ``session_id`` both appear, on different records, and disagreeing on the
    same record is normal around compaction and resume (pass-2 §5.4).
    """
    for key in keys:
        value = _as_str(record.get(key))
        if value:
            return value
    return None


def _as_int(value: Any) -> int:
    """Coerce a recorded token count to a non-negative integer.

    Missing means zero — a turn with no cache read genuinely read nothing. A present but
    non-numeric value is a format change we should not paper over, so it raises.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        raise TranscriptFormatError(f"expected a token count, got a boolean: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise TranscriptFormatError(f"token count is negative: {value}")
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise TranscriptFormatError(f"expected a token count, got {type(value).__name__}: {value!r}")
