"""Ground-truth anchors — reconciling our numbers against Claude Code's own (FR-026).

When a user runs ``/context``, Claude Code writes its output into the transcript as an
``isMeta: true`` user record. That output is an **independent, authoritative token table** for
what was resident at that moment, produced by Anthropic's own tokenizer and broken down per
category, per memory file, per skill, and per MCP tool (pass-2 §1.1). Nobody in the ecosystem
ingests it. It is a free oracle for validating everything ``tokens.py`` computes.

Two rules govern this module, and they are not negotiable:

**We never adjust either side.** The anchor is not a correction factor and our figures are not
evidence the anchor is wrong. Where the records and the reported totals disagree, *the
discrepancy is the finding* (spec, Edge Cases; Principle X).

**A parse failure is reported, not guessed around.** ``/context`` output is human-formatted text
whose layout changes between Claude Code versions. When a record looks like a context report but
yields no entries, this module raises rather than returning an empty table that would read as
"nothing to reconcile".
"""

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccaudit.ingest.records import iter_records

# Category labels Claude Code prints in a `/context` summary. Used only to recognise the record
# — parsing itself is structural, so a renamed or added category still parses.
KNOWN_CONTEXT_LABELS: frozenset[str] = frozenset(
    {
        "system prompt",
        "system tools",
        "mcp tools",
        "memory files",
        "custom agents",
        "messages",
        "free space",
        "autocompact buffer",
        "skills",
    }
)

# Default disagreement threshold, as a fraction of the anchor's own figure.
#
# Why 5%. Three sources of legitimate difference sit between our number and the anchor's, none
# of which is a defect:
#
# 1. **The anchor is printed rounded.** "2.9k" is any value in [2850, 2950) — the printed figure
#    alone carries roughly +/-1.7%, and a bare "3k" carries +/-17%. This is handled exactly,
#    per entry, by `AnchorEntry.display_step` rather than being absorbed into the percentage.
# 2. **Boundary choices differ.** Whether a memory file's tokens include its injection wrapper,
#    and where one category ends and the next begins, are judgement calls the two sides make
#    independently.
# 3. **Tokenizers move between model generations**, and a session can span versions (FR-028).
#
# 5% is tight enough to catch every failure this check exists for — a `chars // 4` image error
# is ~100x, a dedup failure is 2x, a missed cache class is tens of percent — while staying wide
# enough that rounding and boundary choices do not produce a wall of false disagreements that
# trains a reader to ignore it. It is a knob, not a constant of nature: `reconcile()` takes it.
DEFAULT_RELATIVE_TOLERANCE = 0.05

_SECTION_RE = re.compile(r"^#{2,4}\s*(?P<name>.+?)\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^[\s:|-]+$")
_TRAILING_PERCENT_RE = re.compile(r"\(\s*\d+(?:\.\d+)?\s*%\s*\)\s*$")
_TOKEN_FIGURE_RE = re.compile(
    r"^(?P<approx>[~≈]?)\s*(?P<number>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<suffix>[kKmM]?)\s*(?:tokens?)?$"
)
_LABELLED_FIGURE_RE = re.compile(
    r"^(?P<label>[^|]{1,80}?)\s*[:—-]?\s+(?P<figure>[~≈]?\s*[\d,.]+\s*[kKmM]?)"
    r"\s*(?:tokens?)?$"
)


class AnchorParseError(ValueError):
    """A record looked like a ``/context`` report but could not be parsed into a table."""


@dataclass(frozen=True)
class AnchorEntry:
    """One labelled figure from a ``/context`` report, exactly as printed.

    ``display_step`` is the granularity of the printed figure: 1 for ``984``, 100 for ``2.9k``,
    1000 for ``3k``. It is what makes "does this disagree?" answerable without pretending a
    rounded figure is exact.
    """

    label: str
    tokens: int
    display_step: int
    approximate: bool
    section: str | None = None
    qualifiers: tuple[str, ...] = ()
    source_line: int = 0

    def __post_init__(self) -> None:
        if self.tokens < 0:
            raise ValueError(f"anchor {self.label!r} has a negative token count: {self.tokens}")
        if self.display_step < 1:
            raise ValueError(f"anchor {self.label!r} has a non-positive display step")


@dataclass(frozen=True)
class ContextAnchor:
    """A parsed ``/context`` record: where it came from and what it reported."""

    entries: tuple[AnchorEntry, ...]
    line: int = 0
    uuid: str | None = None
    session_id: str | None = None
    timestamp: str | None = None

    def by_label(self) -> dict[str, AnchorEntry]:
        """Entries keyed by label. Later duplicates win, and the caller sees both in ``entries``."""
        return {entry.label: entry for entry in self.entries}


@dataclass(frozen=True)
class AnchorComparison:
    """One computed figure set against its anchor. Neither side is adjusted."""

    label: str
    anchor_tokens: int
    computed_tokens: int
    tolerance: int

    @property
    def delta(self) -> int:
        """Computed minus anchor. Positive means we report more than Claude Code did."""
        return self.computed_tokens - self.anchor_tokens

    @property
    def relative_delta(self) -> float:
        """Delta as a fraction of the anchor. ``inf`` when the anchor is zero and we are not."""
        if self.anchor_tokens == 0:
            return 0.0 if self.computed_tokens == 0 else math.inf
        return self.delta / self.anchor_tokens

    @property
    def within_tolerance(self) -> bool:
        return abs(self.delta) <= self.tolerance

    def describe(self) -> str:
        """One line a reader can check the arithmetic of themselves."""
        verdict = "agrees" if self.within_tolerance else "DISAGREES"
        return (
            f"{self.label}: /context reports {self.anchor_tokens}, we compute "
            f"{self.computed_tokens} (delta {self.delta:+d}, {self.relative_delta:+.1%}, "
            f"tolerance +/-{self.tolerance}) — {verdict}"
        )


@dataclass(frozen=True)
class Reconciliation:
    """The result of checking our figures against an anchor.

    ``unmatched_anchor_labels`` is not a footnote: a category the anchor reports and we never
    accounted for is a gap in our model, and it is shown rather than dropped (FR-019).
    """

    comparisons: tuple[AnchorComparison, ...]
    unmatched_anchor_labels: tuple[str, ...] = ()
    unmatched_computed_labels: tuple[str, ...] = ()
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE

    @property
    def disagreements(self) -> tuple[AnchorComparison, ...]:
        return tuple(c for c in self.comparisons if not c.within_tolerance)

    @property
    def agrees(self) -> bool:
        """True only when every compared figure is within tolerance and nothing is unmatched."""
        return not self.disagreements and not self.unmatched_anchor_labels

    def lines(self) -> list[str]:
        """Human-readable derivation, for ``--explain`` output."""
        rendered = [c.describe() for c in self.comparisons]
        for label in self.unmatched_anchor_labels:
            rendered.append(f"{label}: reported by /context, not accounted for by us — unmatched")
        for label in self.unmatched_computed_labels:
            rendered.append(f"{label}: computed by us, absent from /context — unmatched")
        return rendered


def find_context_anchors(path: Path) -> list[ContextAnchor]:
    """Parse every ``/context`` report in one transcript. Read-only (FR-020).

    Records that merely *look* similar are skipped; a record that is recognised as a context
    report and then fails to parse raises :class:`AnchorParseError`, naming the file and line.
    """
    anchors: list[ContextAnchor] = []
    for line_number, record in iter_records(path):
        text = _context_report_text(record)
        if text is None:
            continue
        try:
            entries = parse_context_report(text)
        except AnchorParseError as error:
            raise AnchorParseError(f"{path.name}:{line_number}: {error}") from None
        anchors.append(
            ContextAnchor(
                entries=entries,
                line=line_number,
                uuid=_as_optional_str(record.get("uuid")),
                session_id=_as_optional_str(record.get("sessionId"))
                or _as_optional_str(record.get("session_id")),
                timestamp=_as_optional_str(record.get("timestamp")),
            )
        )
    return anchors


def parse_context_report(text: str) -> tuple[AnchorEntry, ...]:
    """Turn ``/context`` output into a table of labelled token counts.

    Parsed structurally — section headings, table rows, ``label ... figure`` lines — rather than
    against a fixed layout, because the layout is human-formatted and changes between releases.
    Anything that does not parse is skipped silently *per line*; a report that yields no entries
    at all raises instead of returning an empty table.
    """
    entries: list[AnchorEntry] = []
    section: str | None = None

    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = _SECTION_RE.match(line)
            if heading:
                section = heading.group("name").strip()
            continue
        if line.startswith("|"):
            entry = _parse_table_row(line, section, index)
        else:
            entry = _parse_labelled_line(line, section, index)
        if entry is not None:
            entries.append(entry)

    if not entries:
        raise AnchorParseError(
            "the record was recognised as a /context report but no labelled token figures could "
            "be parsed from it; the report format may have changed. Refusing to return an empty "
            "table, which would read as 'nothing to reconcile'"
        )
    return tuple(entries)


def reconcile(
    entries: Iterable[AnchorEntry],
    computed: Mapping[str, int],
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
) -> Reconciliation:
    """Compare our figures against the anchor's, label by label.

    Matching is by exact label, then case-insensitively. Nothing is scaled, shifted, or dropped
    to make the two sides meet: a disagreement is reported as a disagreement, and a label
    present on only one side is reported as unmatched.
    """
    if relative_tolerance < 0:
        raise ValueError(f"relative tolerance must be non-negative, got {relative_tolerance}")

    remaining = dict(computed)
    lowered = {key.lower(): key for key in computed}
    comparisons: list[AnchorComparison] = []
    unmatched_anchor: list[str] = []

    for entry in entries:
        key = entry.label if entry.label in remaining else lowered.get(entry.label.lower())
        if key is None or key not in remaining:
            unmatched_anchor.append(entry.label)
            continue
        comparisons.append(
            AnchorComparison(
                label=entry.label,
                anchor_tokens=entry.tokens,
                computed_tokens=remaining.pop(key),
                tolerance=tolerance_for(entry, relative_tolerance),
            )
        )

    return Reconciliation(
        comparisons=tuple(comparisons),
        unmatched_anchor_labels=tuple(unmatched_anchor),
        unmatched_computed_labels=tuple(remaining),
        relative_tolerance=relative_tolerance,
    )


def tolerance_for(entry: AnchorEntry, relative_tolerance: float) -> int:
    """Allowed absolute difference for one anchor entry.

    Two terms, and the larger wins. The **relative** term covers boundary choices and tokenizer
    drift. The **display** term covers the fact that the anchor is printed rounded: a figure
    shown as "2.9k" cannot be pinned closer than +/-50 no matter how good our arithmetic is, and
    an approximate "~290" is explicitly marked by Claude Code as not precise.
    """
    relative = math.ceil(relative_tolerance * entry.tokens)
    display = entry.display_step // 2
    if entry.approximate:
        display = max(display, math.ceil(0.1 * entry.tokens))
    return max(relative, display, 0)


def _context_report_text(record: dict[str, Any]) -> str | None:
    """Text of a record that is a ``/context`` report, or ``None`` if it is not one."""
    if record.get("type") != "user" or not record.get("isMeta"):
        return None
    text = _record_text(record)
    if text is None or not _looks_like_context_report(text):
        return None
    return text


def _record_text(record: dict[str, Any]) -> str | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


def _looks_like_context_report(text: str) -> bool:
    """Recognise a context report without depending on its exact layout.

    Either the command is echoed, or at least two of Claude Code's own category labels appear.
    Two rather than one so an ordinary message mentioning "system prompt" is not mistaken for a
    token table.
    """
    lowered = text.lower()
    if "/context" in lowered:
        return True
    return sum(1 for label in KNOWN_CONTEXT_LABELS if label in lowered) >= 2


def _parse_table_row(line: str, section: str | None, source_line: int) -> AnchorEntry | None:
    if _TABLE_SEPARATOR_RE.match(line):
        return None
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    cells = [cell for cell in cells if cell]
    if len(cells) < 2:
        return None

    figure = _parse_token_figure(cells[-1])
    if figure is None:
        return None
    tokens, step, approximate = figure

    # The label column is not in a fixed position across `/context`'s tables: memory files are
    # `Type | Path | Tokens`, skills are `Name | Source | Tokens`. The longest non-figure cell is
    # the identifying one in every observed shape (a path or a skill name beats "User" or
    # "Project"), and ties fall back to the first.
    candidates = cells[:-1]
    label = max(candidates, key=lambda cell: (len(cell), -candidates.index(cell)))
    qualifiers = tuple(cell for cell in candidates if cell != label)
    return AnchorEntry(
        label=label,
        tokens=tokens,
        display_step=step,
        approximate=approximate,
        section=section,
        qualifiers=qualifiers,
        source_line=source_line,
    )


def _parse_labelled_line(line: str, section: str | None, source_line: int) -> AnchorEntry | None:
    stripped = _TRAILING_PERCENT_RE.sub("", line).strip().rstrip(":")
    match = _LABELLED_FIGURE_RE.match(stripped)
    if match is None:
        return None
    label = match.group("label").strip().strip(":").strip()
    if not label or not any(character.isalpha() for character in label):
        return None
    figure = _parse_token_figure(match.group("figure"))
    if figure is None:
        return None
    tokens, step, approximate = figure
    return AnchorEntry(
        label=label,
        tokens=tokens,
        display_step=step,
        approximate=approximate,
        section=section,
        source_line=source_line,
    )


def _parse_token_figure(cell: str) -> tuple[int, int, bool] | None:
    """Parse ``2.9k`` / ``~290`` / ``1,234`` into (tokens, display step, approximate).

    The display step is the granularity the figure was *printed* at, which is what bounds how
    precisely it can be compared against anything.
    """
    text = _TRAILING_PERCENT_RE.sub("", cell).strip()
    match = _TOKEN_FIGURE_RE.match(text)
    if match is None:
        return None
    number = match.group("number").replace(",", "")
    suffix = match.group("suffix").lower()
    approximate = bool(match.group("approx"))

    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    decimals = len(number.split(".")[1]) if "." in number else 0
    try:
        value = float(number) * multiplier
    except ValueError:
        return None
    if value < 0:
        return None

    # A figure printed to `decimals` places at this multiplier resolves no finer than this.
    step = max(1, multiplier // (10**decimals))
    return round(value), step, approximate


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
