"""Contract on the tool's own footprint disclosure (FR-056, SC-017).

This is the project's credibility test on itself. A cost tool that inflated the resident
context would corrupt the baseline it measures, so it has to **measure and disclose** its own
contribution rather than claim it is negligible — and the disclosure has to be honest in the
direction that is uncomfortable for the tool.
"""

from pathlib import Path

import pytest

from claude_cost_tracker.analyse import analyse_transcript
from claude_cost_tracker.config import BUNDLED_PRICING_PATH, load_pricing
from claude_cost_tracker.footprint import FOOTPRINT_CEILING_SHARE, SKILL_PATH, measure
from tests.fixtures.builder import TranscriptBuilder

PRICING = load_pricing(BUNDLED_PRICING_PATH)


def session(tmp_path: Path, turns: int = 10, cost_per_turn: int = 50_000):
    builder = TranscriptBuilder()
    for _ in range(turns):
        builder.add_turn(cache_read=cost_per_turn, output_tokens=100)
    return analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)


class TestTheMeasurement:
    def test_it_measures_the_shipped_skill_description(self, tmp_path: Path) -> None:
        """The description is the *only* always-resident part of the plugin."""
        result = measure(session(tmp_path))
        assert result.description_chars > 0
        assert result.description_tokens > 0

    def test_the_cost_scales_with_how_long_it_was_resident(self, tmp_path: Path) -> None:
        short = measure(session(tmp_path / "a", turns=5))
        long = measure(session(tmp_path / "b", turns=50))
        assert long.cost_micros > short.cost_micros

    def test_it_stays_under_its_own_ceiling_on_a_realistic_session(self, tmp_path: Path) -> None:
        """SC-017. If this ever fails, the tool has become part of the problem."""
        result = measure(session(tmp_path, turns=200))
        assert result.within_ceiling
        assert result.share < FOOTPRINT_CEILING_SHARE

    def test_a_session_with_no_turns_costs_nothing_and_says_why(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_ui_noise(3)
        analysis = analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)
        result = measure(analysis)
        assert result.cost_micros == 0
        assert "nothing to be resident through" in result.method


class TestHonesty:
    def test_the_figure_is_labelled_an_estimate_not_a_measurement(self, tmp_path: Path) -> None:
        result = measure(session(tmp_path))
        assert result.basis == "estimated"
        assert result.confidence == "low"

    def test_the_method_says_how_the_number_was_reached(self, tmp_path: Path) -> None:
        """A figure about ourselves is the one a reader will trust least, and rightly."""
        result = measure(session(tmp_path))
        assert "Character-based" in result.method
        assert "floor" in result.method

    def test_the_disclosure_names_the_ceiling_and_the_verdict(self, tmp_path: Path) -> None:
        text = "\n".join(measure(session(tmp_path)).lines())
        assert "0.5%" in text
        assert "ceiling this tool sets for itself" in text

    def test_the_disclosure_lists_what_is_not_resident(self, tmp_path: Path) -> None:
        """The claim "the rest costs nothing" is the part that needs stating, not assuming."""
        text = "\n".join(measure(session(tmp_path)).lines())
        assert "slash" in text
        assert "SessionEnd hook runs outside the conversation" in text
        assert "no MCP server" in text

    def test_it_admits_the_size_is_not_measured(self, tmp_path: Path) -> None:
        text = "\n".join(measure(session(tmp_path)).lines())
        assert "estimate, not a measurement" in text

    def test_a_breached_ceiling_is_stated_loudly(self, tmp_path: Path) -> None:
        """The uncomfortable case has to be as visible as the comfortable one."""
        tiny = session(tmp_path, turns=1, cost_per_turn=1)
        result = measure(tiny)
        if not result.within_ceiling:
            assert "ABOVE" in "\n".join(result.lines())


class TestFailFast:
    def test_a_missing_skill_file_raises_rather_than_reporting_zero(self, tmp_path: Path) -> None:
        """Reporting a flattering zero from a missing file is the worst available answer."""
        with pytest.raises(FileNotFoundError, match="the thing being measured"):
            measure(session(tmp_path), skill_path=tmp_path / "absent.md")

    def test_a_skill_without_a_description_raises(self, tmp_path: Path) -> None:
        broken = tmp_path / "SKILL.md"
        broken.write_text("---\nname: x\n---\nbody", encoding="utf-8")
        with pytest.raises(ValueError, match="no frontmatter description"):
            measure(session(tmp_path), skill_path=broken)


class TestTheShippedSkill:
    def test_the_shipped_description_stays_small(self) -> None:
        """The one number that has to keep being true as the skill is edited.

        It is resident in every session where the plugin is installed, so growth here is a
        permanent tax on everyone who uses the tool.
        """
        text = SKILL_PATH.read_text(encoding="utf-8")
        description = text.split("description:", 1)[1].split("\n", 1)[0].strip()
        assert len(description) < 600, "the always-resident description is growing"
