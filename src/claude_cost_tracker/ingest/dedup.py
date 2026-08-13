"""Count each recorded exchange exactly once (FR-021).

Claude Code sessions are resumed, forked, and compacted. Each of those copies earlier
assistant messages into a *new* JSONL file, so the same billed exchange is on disk in two or
more places — and re-scanning a corpus that already contains both sums it twice. Every figure
downstream inflates, silently and plausibly, which is the worst failure mode this tool has
(PITFALLS.md: "Transcript records double-count on resume, compact, and fork").

Three properties this module guarantees:

**Dedup happens before any arithmetic.** The identity is :attr:`TurnRecord.dedup_key` —
``(message.id, requestId)`` — the same key ccusage and token-dashboard settled on.

**The output order is deterministic** (FR-017, SC-009). Turns come out in the order they were
first seen: transcripts in the order the caller supplied them, records in file order within
each. *First occurrence wins* — the earliest file to contain an exchange is the one that
originally recorded it, and later copies are the resume/fork/compaction carry-over. The caller
is responsible for supplying transcripts in a stable order; ``discover.py`` sorts, so the
usual path is stable end to end.

**A disagreement is never resolved silently.** Two records sharing a dedup key are supposed to
be byte-identical copies of one API response. When their ``usage`` differs, one of them is
wrong and we cannot tell which, so the conflict is recorded and counted rather than papered
over by an arbitrary pick (Principle I, Principle X).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from claude_cost_tracker.ingest.records import IngestDiagnostic, ParsedTranscript, TurnRecord, Usage

# Diagnostic kinds. Named here so the store and the report cannot drift from the producer.
DUPLICATE_TURN = "duplicate_turn"
USAGE_CONFLICT = "dedup_usage_conflict"


@dataclass(frozen=True)
class TurnOrigin:
    """Where a turn was read from — file and line, enough to reopen and look (Principle VI)."""

    path: Path
    line: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass(frozen=True)
class UsageConflict:
    """Two records claiming the same identity but different charges.

    Kept whole rather than reduced to a count: resolving it needs both sides, and the answer
    is a human's to give.
    """

    dedup_key: tuple[str, str]
    kept: TurnOrigin
    kept_usage: Usage
    rejected: TurnOrigin
    rejected_usage: Usage

    def describe(self) -> str:
        return (
            f"{self.dedup_key[0]}/{self.dedup_key[1]}: {self.kept} charges "
            f"{self.kept_usage.prompt_tokens} prompt + {self.kept_usage.output_tokens} output, "
            f"but {self.rejected} charges {self.rejected_usage.prompt_tokens} prompt + "
            f"{self.rejected_usage.output_tokens} output for the same exchange"
        )


@dataclass
class DedupResult:
    """The turns to do arithmetic on, plus a full account of what was removed and why."""

    turns: list[TurnRecord] = field(default_factory=list)
    origins: dict[tuple[str, str], TurnOrigin] = field(default_factory=dict)
    duplicates_dropped: int = 0
    conflicts: list[UsageConflict] = field(default_factory=list)
    diagnostics: dict[str, IngestDiagnostic] = field(default_factory=dict)

    @property
    def input_count(self) -> int:
        """Turns seen before dedup. ``input_count - duplicates_dropped == len(turns)``."""
        return len(self.turns) + self.duplicates_dropped

    def note(self, kind: str, sample: str) -> None:
        """Record a diagnostic, mirroring :meth:`ParsedTranscript.note` (one pattern only)."""
        self.diagnostics.setdefault(kind, IngestDiagnostic(kind=kind)).record(sample)


def dedup_turns(transcripts: Iterable[ParsedTranscript]) -> DedupResult:
    """Collapse turns from any number of transcripts to one record per exchange.

    Read-only over its inputs. The returned turns are the exact ``TurnRecord`` objects of each
    exchange's first occurrence — nothing is merged or recomputed, so the charges stay the
    observed facts they were parsed as.
    """
    result = DedupResult()
    seen: dict[tuple[str, str], TurnRecord] = {}

    for transcript in transcripts:
        for turn in transcript.turns:
            key = turn.dedup_key
            origin = TurnOrigin(path=transcript.path, line=turn.line)
            first = seen.get(key)
            if first is None:
                seen[key] = turn
                result.origins[key] = origin
                result.turns.append(turn)
                continue

            result.duplicates_dropped += 1
            result.note(DUPLICATE_TURN, f"{origin} duplicates {result.origins[key]}")
            if turn.usage != first.usage:
                # Same exchange, different charges: the copy is not a copy. Surfaced, counted,
                # and left for a human — picking one would invent a number (Principle X).
                conflict = UsageConflict(
                    dedup_key=key,
                    kept=result.origins[key],
                    kept_usage=first.usage,
                    rejected=origin,
                    rejected_usage=turn.usage,
                )
                result.conflicts.append(conflict)
                result.note(USAGE_CONFLICT, conflict.describe())

    return result
