"""Golden test — one session containing everything that breaks a naive parser (SC-014).

Images, a resumed exchange, a subagent, a compaction, unparseable lines, a locally generated
`<synthetic>` notice, and a version boundary, all in one file. The requirement is a **complete
result with no crash and every affected limitation declared** — not a clean result.

A diff here is a red alert, not a rebaseline. See `test_attribution_basic.py` for the rule.
"""

import json
from pathlib import Path

import pytest

from ccaudit.analyse import analyse_transcript
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing
from ccaudit.ingest.records import parse_transcript
from ccaudit.ingest.tokens import resolve_tool_result_tokens

pytestmark = pytest.mark.golden

TRANSCRIPT = Path(__file__).parent / "fixtures" / "session_hostile" / "transcript.jsonl"
PRICING = load_pricing(BUNDLED_PRICING_PATH)


@pytest.fixture(scope="module")
def analysis():
    return analyse_transcript(TRANSCRIPT, pricing=PRICING)


class TestItSurvives:
    def test_it_produces_a_complete_result_without_crashing(self, analysis) -> None:
        assert analysis.total_micros > 0
        assert analysis.reconciliation.adds_up

    def test_it_still_adds_up_exactly(self, analysis) -> None:
        """Invariant A1 holds on the hostile case too, or it does not hold at all."""
        result = analysis.reconciliation
        assert result.attributed_micros + result.unattributed_micros == result.total_micros


class TestImages:
    """The highest-severity trap in the project, pinned.

    Under `chars // 4` a base64 screenshot reports roughly 100x its real token cost and
    dominates every other figure. On the observed corpus that made images look like 95% of all
    tool-result tokens.
    """

    def test_the_image_is_sized_from_its_pixels_not_its_characters(self) -> None:
        parsed = parse_transcript(TRANSCRIPT)
        image_results = [
            record
            for record in parsed.tool_results
            if isinstance(record.payload, dict) and record.payload.get("isImage")
        ]
        assert len(image_results) == 1

        quantity = resolve_tool_result_tokens(image_results[0], "claude-opus-5")
        assert quantity.tokens == 4_784  # 2560x1430/750 saturates the per-image cap
        assert quantity.basis == "measured"

    def test_the_character_count_would_have_been_wildly_wrong(self) -> None:
        """Pins the size of the error, so a regression to characters is unmistakable."""
        raw = TRANSCRIPT.read_text(encoding="utf-8")
        base64_chars = max(
            len(block["source"]["data"])
            for line in raw.splitlines()
            if '"image"' in line
            for block in _image_blocks(line)
        )
        naive = base64_chars // 4
        assert naive > 4_784 * 4, "fixture is too small to catch a character-count regression"


class TestResumeAndSubagents:
    def test_a_replayed_exchange_is_counted_once(self, analysis) -> None:
        """The resume trap. Counting it twice inflates every figure downstream (FR-021)."""
        assert analysis.dedup.duplicates_dropped == 1
        assert len(analysis.timeline.turns) == 5

    def test_subagent_work_is_counted_exactly_once(self, analysis) -> None:
        """FR-009. Its charge is real and stays in the total; it must not also land on the
        parent."""
        assert analysis.attribution.subagent_turns_rolled_up == 1
        output_total = sum(
            a.cost_micros for a in analysis.attribution.attributions if a.component == "output"
        )
        assert output_total == sum(c.output_micros for c in analysis.attribution.charges)


class TestMalformedInput:
    def test_unparseable_records_are_counted_not_dropped(self, analysis) -> None:
        assert analysis.parsed.unparseable_count == 2
        assert any("could not be parsed" in note for note in analysis.limitations)

    def test_a_locally_generated_notice_is_not_billed(self, analysis) -> None:
        """`<synthetic>` records were never sent to the API. They are not turns."""
        assert all(turn.model != "<synthetic>" for turn in analysis.timeline.turns)


class TestDeclaredLimitations:
    def test_the_version_boundary_is_declared(self, analysis) -> None:
        assert analysis.parsed.spans_versions
        assert any("spans Claude Code versions" in note for note in analysis.limitations)

    def test_pre_compaction_clearing_is_declared_as_a_named_residual(self, analysis) -> None:
        """Claude Code clears older tool outputs before compacting, with no marker anywhere.

        It must be shown as its own gap, never folded into carry where it would inflate every
        file's figure.
        """
        assert analysis.timeline.unexplained_dropped_tokens > 0
        assert any("before a compaction" in note for note in analysis.limitations)

    def test_the_figures_are_labelled_as_estimates(self, analysis) -> None:
        joined = " ".join(analysis.limitations)
        assert "API-equivalent" in joined
        assert "billed" not in joined.replace("not billed amounts", "")


class TestStability:
    def test_the_hostile_session_is_deterministic(self) -> None:
        first = analyse_transcript(TRANSCRIPT, pricing=PRICING)
        second = analyse_transcript(TRANSCRIPT, pricing=PRICING)
        assert first.total_micros == second.total_micros
        assert first.reconciliation.unattributed_micros == (
            second.reconciliation.unattributed_micros
        )


def _image_blocks(line: str) -> list[dict]:
    record = json.loads(line)
    content = record.get("message", {}).get("content", [])
    blocks = []
    for block in content:
        for inner in block.get("content", []) if isinstance(block.get("content"), list) else []:
            if isinstance(inner, dict) and inner.get("type") == "image":
                blocks.append(inner)
    return blocks
