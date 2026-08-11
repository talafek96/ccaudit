"""Contract on ground-truth reconciliation against ``/context`` (FR-026).

Two invariants are fenced here. **A disagreement is reported, never absorbed** — neither side is
scaled or nudged to make the numbers meet, because the discrepancy is the finding. And **a parse
failure is reported, never silently returned as an empty table**, which would read as "nothing
to reconcile" and quietly retire the only independent check the tool has.
"""

import json
from pathlib import Path

import pytest

from ccaudit.ingest.anchors import (
    DEFAULT_RELATIVE_TOLERANCE,
    AnchorEntry,
    AnchorParseError,
    find_context_anchors,
    parse_context_report,
    reconcile,
    tolerance_for,
)

CONTEXT_REPORT = """\
> /context

Context Usage

| Category | Tokens |
| --- | --- |
| System prompt | 2.9k |
| System tools | 18.6k |
| MCP tools | 16.8k |
| Memory files | 984 |
| Messages | 147.3k |

### Memory Files
| Type | Path | Tokens |
| User | /Users/dev/.claude/CLAUDE.md | 130 |
| Project | /Users/dev/projects/ccaudit/CLAUDE.md | 531 |

### Skills
| carousel-composition | Project | ~290 |
| dataviz | Built-in | ~380 |
"""


def context_record(text: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "type": "user",
        "isMeta": True,
        "uuid": "anchor-uuid",
        "sessionId": "session-1",
        "timestamp": "2026-08-11T10:00:00Z",
        "message": {"role": "user", "content": text},
    }
    record.update(overrides)
    return record


def write_transcript(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


class TestParsing:
    def test_parses_the_summary_table(self) -> None:
        entries = {entry.label: entry for entry in parse_context_report(CONTEXT_REPORT)}
        assert entries["System prompt"].tokens == 2900
        assert entries["System tools"].tokens == 18_600
        assert entries["Memory files"].tokens == 984
        assert entries["Messages"].tokens == 147_300

    def test_parses_the_per_file_detail_table_under_its_section(self) -> None:
        """The path is the identifying column, not the `Type` cell beside it."""
        entries = {entry.label: entry for entry in parse_context_report(CONTEXT_REPORT)}
        memory = entries["/Users/dev/projects/ccaudit/CLAUDE.md"]
        assert memory.tokens == 531
        assert memory.section == "Memory Files"
        assert memory.qualifiers == ("Project",)

    def test_records_the_printed_granularity_of_each_figure(self) -> None:
        """A figure shown as `2.9k` cannot be pinned closer than +/-50, however good we are."""
        entries = {entry.label: entry for entry in parse_context_report(CONTEXT_REPORT)}
        assert entries["System prompt"].display_step == 100
        assert entries["Memory files"].display_step == 1
        assert entries["carousel-composition"].approximate is True

    def test_parses_a_plain_label_and_figure_line(self) -> None:
        text = "Context Usage\nSystem prompt: 2.9k\nMemory files 984 tokens (0.5%)\n"
        entries = {entry.label: entry.tokens for entry in parse_context_report(text)}
        assert entries == {"System prompt": 2900, "Memory files": 984}

    def test_a_malformed_report_raises_rather_than_returning_nothing(self) -> None:
        with pytest.raises(AnchorParseError, match="no labelled token figures"):
            parse_context_report("> /context\n\nsomething went wrong rendering the table\n")


class TestDiscovery:
    def test_finds_a_context_record_in_a_transcript(self, tmp_path: Path) -> None:
        path = write_transcript(
            tmp_path / "session.jsonl",
            [
                {"type": "assistant", "uuid": "a"},
                context_record(CONTEXT_REPORT),
            ],
        )
        anchors = find_context_anchors(path)
        assert len(anchors) == 1
        assert anchors[0].line == 2
        assert anchors[0].session_id == "session-1"
        assert anchors[0].by_label()["Memory files"].tokens == 984

    def test_ignores_a_normal_user_message(self, tmp_path: Path) -> None:
        """Only `isMeta` records that actually look like a token table are anchors."""
        path = write_transcript(
            tmp_path / "session.jsonl",
            [context_record("please summarise the system prompt for me", isMeta=False)],
        )
        assert find_context_anchors(path) == []

    def test_a_recognised_but_unparseable_record_names_the_file_and_line(
        self, tmp_path: Path
    ) -> None:
        path = write_transcript(
            tmp_path / "session.jsonl", [context_record("> /context\n\n(rendering failed)\n")]
        )
        with pytest.raises(AnchorParseError, match=r"session\.jsonl:1"):
            find_context_anchors(path)


class TestReconciliation:
    def test_a_deliberate_mismatch_is_reported_not_absorbed(self) -> None:
        entries = parse_context_report(CONTEXT_REPORT)
        result = reconcile(entries, {"Memory files": 2000, "System prompt": 2900})
        disagreements = {c.label: c for c in result.disagreements}
        assert set(disagreements) == {"Memory files"}
        assert disagreements["Memory files"].anchor_tokens == 984
        assert disagreements["Memory files"].computed_tokens == 2000
        assert disagreements["Memory files"].delta == 1016
        assert not result.agrees

    def test_neither_side_is_adjusted_to_match_the_other(self) -> None:
        entries = (AnchorEntry(label="Messages", tokens=1000, display_step=1, approximate=False),)
        result = reconcile(entries, {"Messages": 100_000})
        comparison = result.comparisons[0]
        assert comparison.anchor_tokens == 1000
        assert comparison.computed_tokens == 100_000
        assert comparison.relative_delta == pytest.approx(99.0)

    def test_rounding_in_the_printed_figure_is_not_a_disagreement(self) -> None:
        entries = (
            AnchorEntry(label="System prompt", tokens=2900, display_step=100, approximate=False),
        )
        result = reconcile(entries, {"System prompt": 2949})
        assert result.agrees

    def test_a_category_the_anchor_reports_and_we_never_accounted_for_is_shown(self) -> None:
        entries = parse_context_report(CONTEXT_REPORT)
        result = reconcile(entries, {"Memory files": 984})
        assert "System prompt" in result.unmatched_anchor_labels
        assert not result.agrees, "an unaccounted-for category is a gap, not a pass"

    def test_a_figure_we_compute_that_the_anchor_never_mentions_is_shown(self) -> None:
        entries = (AnchorEntry(label="Messages", tokens=100, display_step=1, approximate=False),)
        result = reconcile(entries, {"Messages": 100, "Invented category": 50})
        assert result.unmatched_computed_labels == ("Invented category",)

    def test_label_matching_is_case_insensitive(self) -> None:
        entries = (
            AnchorEntry(label="Memory files", tokens=984, display_step=1, approximate=False),
        )
        result = reconcile(entries, {"memory files": 984})
        assert result.agrees

    def test_every_comparison_describes_its_own_arithmetic(self) -> None:
        """A finding must be reproducible by the reader without rerunning the tool."""
        entries = (AnchorEntry(label="Messages", tokens=1000, display_step=1, approximate=False),)
        line = reconcile(entries, {"Messages": 1500}).lines()[0]
        assert "1000" in line and "1500" in line and "+500" in line
        assert "DISAGREES" in line

    def test_a_negative_tolerance_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            reconcile((), {}, relative_tolerance=-0.1)


class TestTolerance:
    def test_default_is_five_percent(self) -> None:
        assert DEFAULT_RELATIVE_TOLERANCE == 0.05

    def test_the_relative_term_governs_large_figures(self) -> None:
        entry = AnchorEntry(label="Messages", tokens=100_000, display_step=100, approximate=False)
        assert tolerance_for(entry, 0.05) == 5000

    def test_the_display_term_governs_a_coarsely_printed_small_figure(self) -> None:
        """`3k` is anything in [2500, 3500); 5% of it would be a tighter bound than the print."""
        entry = AnchorEntry(label="Skills", tokens=3000, display_step=1000, approximate=False)
        assert tolerance_for(entry, 0.05) == 500

    def test_an_approximate_figure_gets_at_least_ten_percent(self) -> None:
        entry = AnchorEntry(label="dataviz", tokens=380, display_step=10, approximate=True)
        assert tolerance_for(entry, 0.05) == 38
