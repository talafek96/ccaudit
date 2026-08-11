"""Synthetic transcript builder for fixtures.

**Synthetic or scrubbed, never real.** A real transcript carries file paths, shell commands,
and source from whatever sessions produced it, so committing one would leak whatever was on
the developer's machine (git-conventions: what never gets committed). Everything here is
fabricated, and the shapes are modelled on the schema verified against a real corpus in
`docs/research/prior-art-pass-2.md` §5 — including the parts that are easy to get wrong:
``cache_creation`` as a per-TTL dict, ``compact_boundary`` carrying ``logicalParentUuid``
instead of ``parentUuid``, and the ~60% of records that are UI state rather than conversation.

A fixture that only contains clean assistant turns would fence nothing. The builder emits the
noise too, on purpose.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_VERSION = "2.1.220"
DEFAULT_SESSION_ID = "11111111-2222-3333-4444-555555555555"


@dataclass
class TranscriptBuilder:
    """Builds a JSONL transcript one record at a time.

    Every ``add_*`` returns the record's uuid so a test can thread parent links, and each
    method appends to ``records`` in order — the file is written exactly as built.
    """

    session_id: str = DEFAULT_SESSION_ID
    version: str = DEFAULT_VERSION
    project_path: str = "/Users/dev/projects/demo"
    records: list[dict[str, Any]] = field(default_factory=list)
    _counter: int = 0
    _last_uuid: str | None = None

    def next_uuid(self, prefix: str = "rec") -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    # -- conversation ---------------------------------------------------------------------

    def add_turn(
        self,
        *,
        model: str = DEFAULT_MODEL,
        input_tokens: int = 0,
        cache_creation_5m: int = 0,
        cache_creation_1h: int = 0,
        cache_read: int = 0,
        output_tokens: int = 0,
        message_id: str | None = None,
        request_id: str | None = None,
        tool_use_ids: tuple[str, ...] = (),
        is_sidechain: bool = False,
        agent_id: str | None = None,
        attribution_skill: str | None = None,
        attribution_agent: str | None = None,
        version: str | None = None,
        uuid: str | None = None,
        parent_uuid: str | None = None,
    ) -> str:
        """An assistant record — the only record type that carries billable usage."""
        record_uuid = uuid or self.next_uuid("asst")
        content: list[dict[str, Any]] = [{"type": "text", "text": "..."}]
        content.extend(
            {"type": "tool_use", "id": tool_id, "name": "Read", "input": {}}
            for tool_id in tool_use_ids
        )
        record: dict[str, Any] = {
            "type": "assistant",
            "uuid": record_uuid,
            "parentUuid": parent_uuid if parent_uuid is not None else self._last_uuid,
            "sessionId": self.session_id,
            "timestamp": self._timestamp(),
            "version": version if version is not None else self.version,
            "requestId": request_id or f"req_{self._counter:04d}",
            "message": {
                "id": message_id or f"msg_{self._counter:04d}",
                "model": model,
                "role": "assistant",
                "content": content,
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": cache_creation_5m,
                        "ephemeral_1h_input_tokens": cache_creation_1h,
                    },
                    "cache_creation_input_tokens": cache_creation_5m + cache_creation_1h,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": output_tokens,
                    "service_tier": "standard",
                },
            },
        }
        if is_sidechain:
            record["isSidechain"] = True
            record["agentId"] = agent_id or "agent-1"
        if attribution_skill:
            record["attributionSkill"] = attribution_skill
        if attribution_agent:
            record["attributionAgent"] = attribution_agent
        return self._append(record, record_uuid)

    def add_tool_result(
        self,
        *,
        tool_use_id: str,
        text: str = "file contents",
        tool_name: str = "Read",
        file_path: str | None = None,
        num_lines: int | None = None,
        total_lines: int | None = None,
        is_sidechain: bool = False,
    ) -> str:
        """A user record returning a tool result — the common route content enters context."""
        record_uuid = self.next_uuid("tres")
        payload: Any
        if tool_name == "Read" and file_path:
            payload = {
                "type": "text",
                "file": {
                    "filePath": file_path,
                    "content": text,
                    "numLines": num_lines if num_lines is not None else text.count("\n") + 1,
                    "startLine": 1,
                    "totalLines": total_lines if total_lines is not None else text.count("\n") + 1,
                },
            }
        else:
            payload = {"stdout": text, "stderr": "", "interrupted": False, "isImage": False}
        record: dict[str, Any] = {
            "type": "user",
            "uuid": record_uuid,
            "parentUuid": self._last_uuid,
            "sessionId": self.session_id,
            "timestamp": self._timestamp(),
            "version": self.version,
            "toolName": tool_name,
            "toolUseResult": payload,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": text},
                ],
            },
        }
        if is_sidechain:
            record["isSidechain"] = True
        return self._append(record, record_uuid)

    def add_image_tool_result(self, *, tool_use_id: str, base64_data: str) -> str:
        """A tool result whose payload is an image.

        The single highest-severity parser trap: sized by character count, a base64 image
        reports ~100x its real token cost and swamps every other figure in the report.
        """
        record_uuid = self.next_uuid("timg")
        record: dict[str, Any] = {
            "type": "user",
            "uuid": record_uuid,
            "parentUuid": self._last_uuid,
            "sessionId": self.session_id,
            "timestamp": self._timestamp(),
            "version": self.version,
            "toolUseResult": {"isImage": True},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64_data,
                                },
                            }
                        ],
                    }
                ],
            },
        }
        return self._append(record, record_uuid)

    def add_user_text(self, text: str = "do the thing", *, is_meta: bool = False) -> str:
        """Real user typing, or an isMeta sidecar. Neither is an attributable injection."""
        record_uuid = self.next_uuid("user")
        record: dict[str, Any] = {
            "type": "user",
            "uuid": record_uuid,
            "parentUuid": self._last_uuid,
            "sessionId": self.session_id,
            "timestamp": self._timestamp(),
            "version": self.version,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        if is_meta:
            record["isMeta"] = True
        return self._append(record, record_uuid)

    # -- attachments — the residency ledger tool-call walks miss --------------------------

    def add_attachment(self, attachment_type: str, payload: dict[str, Any] | None = None) -> str:
        record_uuid = self.next_uuid("attach")
        attachment: dict[str, Any] = {"type": attachment_type}
        attachment.update(payload or {})
        record = {
            "type": "attachment",
            "uuid": record_uuid,
            "parentUuid": self._last_uuid,
            "sessionId": self.session_id,
            "timestamp": self._timestamp(),
            "version": self.version,
            "attachment": attachment,
        }
        return self._append(record, record_uuid)

    def add_at_mention(self, *, display_path: str, content: str) -> str:
        """An ``@``-mentioned file: no tool call, no file_path — invisible to a naive walk."""
        return self.add_attachment(
            "file",
            {
                "filename": Path(display_path).name,
                "displayPath": display_path,
                "content": {"file": {"filePath": display_path, "content": content}},
            },
        )

    def add_skill_listing(self, names: list[str], content: str) -> str:
        return self.add_attachment(
            "skill_listing",
            {"names": names, "skillCount": len(names), "isInitial": True, "content": content},
        )

    def add_tool_schema_delta(
        self, *, added: list[str], added_lines: int = 0, removed: list[str] | None = None
    ) -> str:
        """Tool/MCP schemas entering or leaving context — the largest resident block."""
        return self.add_attachment(
            "deferred_tools_delta",
            {
                "addedNames": added,
                "addedLines": added_lines,
                "removedNames": removed or [],
                "readdedNames": [],
                "pendingMcpServers": [],
            },
        )

    # -- boundaries and noise -------------------------------------------------------------

    def add_compaction(
        self,
        *,
        pre_tokens: int,
        post_tokens: int,
        preserved_uuids: list[str],
        trigger: str = "auto",
        cumulative_dropped: int | None = None,
    ) -> str:
        """A compaction boundary.

        Note the shape: ``parentUuid`` is null and ``logicalParentUuid`` carries the real
        link. A DAG walk following only ``parentUuid`` splits the session into disconnected
        components at every compaction.
        """
        record_uuid = self.next_uuid("compact")
        record = {
            "type": "system",
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "uuid": record_uuid,
            "parentUuid": None,
            "logicalParentUuid": self._last_uuid,
            "sessionId": self.session_id,
            "timestamp": self._timestamp(),
            "version": self.version,
            "compactMetadata": {
                "trigger": trigger,
                "preTokens": pre_tokens,
                "postTokens": post_tokens,
                "cumulativeDroppedTokens": (
                    cumulative_dropped
                    if cumulative_dropped is not None
                    else max(0, pre_tokens - post_tokens)
                ),
                "durationMs": 1000,
                "preservedMessages": {
                    "anchorUuid": preserved_uuids[0] if preserved_uuids else None,
                    "uuids": preserved_uuids,
                    "allUuids": preserved_uuids,
                },
            },
        }
        return self._append(record, record_uuid)

    def add_ui_noise(self, count: int = 5) -> None:
        """UI-state records. ~60% of a real transcript is this; a parser must not choke."""
        types = ["last-prompt", "mode", "ai-title", "queue-operation", "file-history-snapshot"]
        for index in range(count):
            record_uuid = self.next_uuid("noise")
            self._append(
                {
                    "type": types[index % len(types)],
                    "uuid": record_uuid,
                    "sessionId": self.session_id,
                    "timestamp": self._timestamp(),
                },
                record_uuid,
                advance=False,
            )

    def add_raw(self, record: dict[str, Any]) -> None:
        """Append an arbitrary record, for malformed-input tests."""
        self.records.append(record)

    def add_malformed_line(self) -> None:
        """A line that is not JSON at all. Must be counted, never silently skipped."""
        self.records.append({"__raw_line__": "{not json at all"})

    # -- output ---------------------------------------------------------------------------

    def to_jsonl(self) -> str:
        lines = []
        for record in self.records:
            raw = record.get("__raw_line__")
            lines.append(raw if isinstance(raw, str) else json.dumps(record))
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8")
        return path

    def write_to_project_tree(self, root: Path, filename: str | None = None) -> Path:
        """Write into a `~/.claude`-shaped tree, for discovery tests.

        Claude Code encodes the project path into the directory name by replacing separators
        with hyphens — a lossy encoding that cannot be reversed unambiguously.
        """
        encoded = self.project_path.replace("/", "-")
        target = root / "projects" / encoded / (filename or f"{self.session_id}.jsonl")
        return self.write(target)

    def _append(self, record: dict[str, Any], record_uuid: str, *, advance: bool = True) -> str:
        self.records.append(record)
        if advance:
            self._last_uuid = record_uuid
        return record_uuid

    def _timestamp(self) -> str:
        # Fixed base so fixtures are byte-reproducible; the clock is never read here.
        return f"2026-08-11T10:{self._counter % 60:02d}:00.000Z"


def simple_session(**kwargs: Any) -> TranscriptBuilder:
    """A small, well-formed session: a read, a turn that carries it, and some UI noise."""
    builder = TranscriptBuilder(**kwargs)
    builder.add_user_text("read the config")
    builder.add_turn(
        input_tokens=100, cache_creation_5m=2000, output_tokens=50, tool_use_ids=("t1",)
    )
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/src/app.py", text="x = 1\n")
    builder.add_ui_noise(3)
    builder.add_turn(input_tokens=10, cache_read=2100, output_tokens=120)
    return builder
