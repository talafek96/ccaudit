"""Contract on `--explain`.

The bar is SC-008: a skeptical reader traces any figure back to the records that produced it
**without rerunning the tool**. So these tests check that a trace carries enough to redo the
arithmetic by hand — not merely that it prints something.
"""

from pathlib import Path

import pytest

from claude_cost_tracker.analyse import SessionAnalysis, analyse_transcript
from claude_cost_tracker.config import BUNDLED_PRICING_PATH, load_pricing
from claude_cost_tracker.money import format_micros
from claude_cost_tracker.render.explain import (
    UnknownFigureError,
    available_figures,
    explain,
    explain_total,
)
from tests.fixtures.builder import TranscriptBuilder

PRICING = load_pricing(BUNDLED_PRICING_PATH)
GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "session_basic"


@pytest.fixture(scope="module")
def analysis() -> SessionAnalysis:
    return analyse_transcript(GOLDEN / "transcript.jsonl", pricing=PRICING)


@pytest.fixture(scope="module")
def unexplained_analysis(tmp_path_factory: pytest.TempPathFactory) -> SessionAnalysis:
    """A session whose cache write is far larger than the content that arrived to cause it.

    This is the ordinary shape of a real session, not a corner case: the first write pays for
    the system prompt and every tool schema, none of which appears in the transcript. The
    golden fixture is deliberately the opposite — small and fully explained — so the gap
    needs its own session to be exercised at all.
    """
    builder = TranscriptBuilder()
    builder.add_user_text("start")
    builder.add_turn(
        input_tokens=5, cache_creation_5m=60_000, output_tokens=40, tool_use_ids=("t1",)
    )
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/small.py", text="x = 1\n" * 20)
    builder.add_turn(input_tokens=5, cache_read=60_000, output_tokens=40)
    path = tmp_path_factory.mktemp("unexplained") / "s.jsonl"
    return analyse_transcript(builder.write(path), pricing=PRICING)


@pytest.fixture(scope="module")
def unexplained_write(unexplained_analysis: SessionAnalysis) -> str:
    return explain_total(unexplained_analysis).render()


class TestFigureLookup:
    def test_every_attributed_figure_is_explainable(self, analysis: SessionAnalysis) -> None:
        figures = available_figures(analysis)
        assert figures
        for handle in figures:
            assert explain(analysis, handle).lines

    def test_a_reader_can_use_the_path_rather_than_the_internal_id(
        self, analysis: SessionAnalysis
    ) -> None:
        """Nobody types `carry:file:/repo/src/a.py` out of a report."""
        trace = explain(analysis, "carry:file:/repo/src/a.py")
        assert "a.py" in trace.render()

    def test_an_ambiguous_handle_says_what_it_matched(self, analysis: SessionAnalysis) -> None:
        with pytest.raises(UnknownFigureError, match="matches"):
            explain(analysis, "a.py")

    def test_an_unknown_handle_lists_what_is_available(self, analysis: SessionAnalysis) -> None:
        """An empty result is a normal outcome the caller acts on, not a crash."""
        with pytest.raises(UnknownFigureError, match="Available"):
            explain(analysis, "nonexistent.py")


@pytest.fixture(scope="module")
def trace_text(analysis: SessionAnalysis) -> str:
    return explain(analysis, "carry:file:/repo/src/a.py").render()


@pytest.fixture(scope="module")
def total_text(analysis: SessionAnalysis) -> str:
    return explain_total(analysis).render()


class TestTraceContent:
    def test_it_names_the_component_in_plain_language(self, trace_text: str) -> None:
        assert "Keeping context loaded" in trace_text

    def test_it_keeps_the_technical_term_as_a_secondary_label(self, trace_text: str) -> None:
        assert "carry" in trace_text

    def test_it_states_the_basis_and_confidence(self, trace_text: str) -> None:
        assert "Basis:" in trace_text
        assert "confidence" in trace_text

    def test_it_states_the_formula(self, trace_text: str) -> None:
        assert "Formula:" in trace_text

    def test_it_names_the_policy_a_carry_figure_rests_on(self, trace_text: str) -> None:
        """A carry number is one defensible division of a shared charge, and must say so."""
        assert "policy" in trace_text
        assert "proportion" in trace_text

    def test_it_pairs_the_figure_with_its_share(self, trace_text: str) -> None:
        assert "%" in trace_text
        assert "of session total" in trace_text

    def test_it_cites_the_source_records(self, trace_text: str) -> None:
        """Without these the reader cannot check anything (FR-015)."""
        assert "Source records:" in trace_text
        assert "@line" in trace_text

    def test_it_reports_the_cause_profile(self, trace_text: str) -> None:
        """Loaded how many times, resident how long — the two causes with opposite fixes."""
        assert "loaded" in trace_text
        assert "resident for" in trace_text

    def test_it_does_not_print_finer_than_the_confidence_supports(
        self, analysis: SessionAnalysis
    ) -> None:
        trace = explain(analysis, "carry:file:/repo/src/a.py")
        assert "significant figures" in trace.render()


class TestOutputFigures:
    def test_an_output_figure_explains_why_it_touches_no_file(
        self, analysis: SessionAnalysis
    ) -> None:
        trace = explain(analysis, "output:session")
        text = trace.render()
        assert "never to a file" in text
        assert "What Claude wrote back" in text


class TestSessionTotal:
    def test_it_breaks_the_total_into_the_four_components(self, total_text: str) -> None:
        assert "loading into context" in total_text
        assert "keeping context loaded" in total_text
        assert "your new typing" in total_text
        assert "what Claude wrote back" in total_text

    def test_it_says_the_figure_is_not_a_bill(self, total_text: str) -> None:
        assert "not a bill" in total_text
        assert "API-equivalent" in total_text

    def test_it_names_the_rate_table_and_its_date(self, total_text: str) -> None:
        """How old the rates are is part of the number's basis."""
        assert "Rates:" in total_text
        assert PRICING.priced_on in total_text

    def test_it_shows_the_unattributed_remainder(self, total_text: str) -> None:
        assert "could not be attributed" in total_text

    def test_it_lists_the_limitations(self, total_text: str) -> None:
        assert "Limitations" in total_text
        assert "stripped before the transcript" in total_text

    def test_it_says_which_charge_the_remainder_sits_in(self, unexplained_write: str) -> None:
        """A bare "33% could not be attributed" is indistinguishable from a broken tool.

        The remainder is overwhelmingly cache-write cost, because a write also pays for the
        resident instruction block that never reaches the transcript. Saying so is what
        makes a large remainder readable as an honest refusal rather than a defect.
        """
        prose = " ".join(unexplained_write.split())
        assert "charged for cache writes" in prose
        assert "was tied to a named item" in prose
        assert "resident instruction block" in prose
        assert "More tools or MCP servers make this block" in prose

    def test_the_breakdown_is_absent_when_every_write_is_explained(self, total_text: str) -> None:
        """It reports a gap; it does not manufacture one. The golden fixture has none."""
        assert "charged for cache writes" not in total_text

    def test_the_remainder_breakdown_is_arithmetic_the_reader_can_redo(
        self, unexplained_analysis: SessionAnalysis
    ) -> None:
        """SC-008: the two parts must be stated, and must account for the write charge."""
        analysis = unexplained_analysis
        charges = analysis.attribution.charges
        write = sum(charge.cache_write_micros for charge in charges)
        direct = sum(
            row.cost_micros
            for row in analysis.attribution.attributions
            if row.component == "direct"
        )
        prose = " ".join(explain_total(analysis).render().split())
        assert format_micros(write, 2) in prose
        assert format_micros(direct, 2) in prose
        assert format_micros(write - direct, 2) in prose


class TestReproducibility:
    def test_the_trace_is_identical_across_runs(self, analysis: SessionAnalysis) -> None:
        first = explain(analysis, "carry:file:/repo/docs/b.md").render()
        second = explain(analysis, "carry:file:/repo/docs/b.md").render()
        assert first == second

    def test_the_figure_in_the_trace_matches_the_hand_verified_golden(
        self, analysis: SessionAnalysis
    ) -> None:
        """333 micro-dollars, derived by hand in the golden's expected.md.

        Sub-cent, so it renders as `<$0.01` rather than a false `$0.00` — the reader is told
        it is small, not told it is nothing.
        """
        assert "<$0.01" in explain(analysis, "carry:file:/repo/docs/b.md").render()
