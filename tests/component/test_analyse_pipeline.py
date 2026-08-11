"""The analysis stage end-to-end over fixture data — the primary test level (Principle V).

One process, real pipeline, fixture transcripts, injected pricing. What is under test is the
composition: parse, dedup, size, timeline, attribute, reconcile, in that order, producing a
breakdown that adds up.
"""

from pathlib import Path

import pytest

from ccaudit.analyse import analyse_transcript
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing
from tests.fixtures.builder import TranscriptBuilder, simple_session

PRICING = load_pricing(BUNDLED_PRICING_PATH)


def busy_session() -> TranscriptBuilder:
    """A session with the shapes that actually break things, not just a happy path.

    The files are deliberately **above** the 512-token cacheability minimum for this model. A
    fixture built from smaller files puts every item in the sub-threshold lane, where carry is
    never shared — which quietly makes any test about carry splitting vacuous.
    """
    builder = TranscriptBuilder()
    builder.add_user_text("audit the config")
    builder.add_turn(
        input_tokens=420, cache_creation_1h=12_000, output_tokens=310, tool_use_ids=("t1",)
    )
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/src/app.py", text="x = 1\n" * 2_000)
    builder.add_ui_noise(4)
    builder.add_turn(input_tokens=37, cache_read=12_400, output_tokens=95, tool_use_ids=("t2",))
    builder.add_tool_result(
        tool_use_id="t2", file_path="/repo/docs/guide.md", text="# Guide\n" * 1_200
    )
    builder.add_at_mention(display_path="/repo/CLAUDE.md", content="# Rules\n" * 400)
    builder.add_turn(input_tokens=11, cache_creation_5m=3_100, cache_read=12_400, output_tokens=71)
    builder.add_turn(input_tokens=9, cache_read=15_500, output_tokens=1_204, is_sidechain=True)
    builder.add_turn(input_tokens=3, cache_read=15_500, output_tokens=44)
    return builder


class TestReconciliation:
    """The core promise: the parts equal the whole, exactly (SC-001)."""

    @pytest.mark.parametrize("policy", ["proportional", "exclusive"])
    def test_the_breakdown_adds_up_exactly(self, policy: str, tmp_path: Path) -> None:
        analysis = analyse_transcript(
            busy_session().write(tmp_path / "s.jsonl"), pricing=PRICING, policy=policy
        )
        result = analysis.reconciliation
        assert result.attributed_micros + result.unattributed_micros == result.total_micros
        assert result.adds_up

    def test_it_adds_up_on_a_trivial_session_too(self, tmp_path: Path) -> None:
        analysis = analyse_transcript(simple_session().write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert analysis.reconciliation.adds_up

    def test_a_session_with_no_turns_reconciles_at_zero(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_ui_noise(10)
        analysis = analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert analysis.total_micros == 0
        assert analysis.reconciliation.adds_up

    def test_a_session_with_no_file_activity_is_valid_not_empty(self, tmp_path: Path) -> None:
        """Dominated by resident content, not an error state (edge case)."""
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=100, cache_creation_5m=50_000, output_tokens=200)
        builder.add_turn(input_tokens=10, cache_read=50_000, output_tokens=100)
        analysis = analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert analysis.total_micros > 0
        assert analysis.reconciliation.adds_up

    def test_the_unattributed_share_is_always_available(self, tmp_path: Path) -> None:
        analysis = analyse_transcript(busy_session().write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert 0.0 <= analysis.reconciliation.unattributed_share <= 1.0


class TestPolicyEffect:
    def test_the_policy_moves_per_item_figures_but_never_the_total(self, tmp_path: Path) -> None:
        """A policy divides a fixed pool; it cannot change what the session cost."""
        path = busy_session().write(tmp_path / "s.jsonl")
        proportional = analyse_transcript(path, pricing=PRICING, policy="proportional")
        exclusive = analyse_transcript(path, pricing=PRICING, policy="exclusive")

        assert proportional.total_micros == exclusive.total_micros
        assert (
            exclusive.reconciliation.unattributed_micros
            > proportional.reconciliation.unattributed_micros
        )


class TestDeterminism:
    def test_the_same_transcript_yields_identical_figures(self, tmp_path: Path) -> None:
        """FR-017, SC-009 — byte-identical across runs, and therefore across machines."""
        path = busy_session().write(tmp_path / "s.jsonl")
        first = analyse_transcript(path, pricing=PRICING)
        second = analyse_transcript(path, pricing=PRICING)

        assert first.total_micros == second.total_micros
        assert [
            (a.target_id, a.component, a.cost_micros) for a in first.attribution.attributions
        ] == [(a.target_id, a.component, a.cost_micros) for a in second.attribution.attributions]

    def test_analysis_never_modifies_the_transcript(self, tmp_path: Path) -> None:
        """~/.claude/ is read-only input (FR-020)."""
        path = busy_session().write(tmp_path / "s.jsonl")
        before = path.read_bytes()
        analyse_transcript(path, pricing=PRICING)
        assert path.read_bytes() == before


class TestDedupInThePipeline:
    def test_a_duplicated_exchange_does_not_double_the_total(self, tmp_path: Path) -> None:
        """The resume/fork trap, fenced at the pipeline level (FR-021)."""
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=100, output_tokens=500, message_id="m1", request_id="r1")
        once = analyse_transcript(builder.write(tmp_path / "once.jsonl"), pricing=PRICING)

        builder.add_turn(input_tokens=100, output_tokens=500, message_id="m1", request_id="r1")
        twice = analyse_transcript(builder.write(tmp_path / "twice.jsonl"), pricing=PRICING)

        assert twice.total_micros == once.total_micros
        assert twice.dedup.duplicates_dropped == 1


class TestLimitations:
    def test_every_analysis_states_that_costs_are_imputed(self, tmp_path: Path) -> None:
        """FR-010 — never worded as a bill, on any surface."""
        analysis = analyse_transcript(busy_session().write(tmp_path / "s.jsonl"), pricing=PRICING)
        joined = " ".join(analysis.limitations)
        assert "API-equivalent" in joined
        assert "not billed amounts" in joined

    def test_it_says_which_rate_table_priced_the_figures(self, tmp_path: Path) -> None:
        analysis = analyse_transcript(busy_session().write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert any(PRICING.priced_on in note for note in analysis.limitations)

    def test_it_declares_that_some_resident_content_is_absent_from_the_records(
        self, tmp_path: Path
    ) -> None:
        """FR-018 — the records under-report exactly the content under dispute."""
        analysis = analyse_transcript(busy_session().write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert any("stripped before the transcript" in note for note in analysis.limitations)

    def test_unparseable_records_are_declared_not_hidden(self, tmp_path: Path) -> None:
        builder = busy_session()
        builder.add_malformed_line()
        analysis = analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert any("could not be parsed" in note for note in analysis.limitations)
        assert analysis.diagnostics

    def test_pre_compaction_clearing_is_declared_when_it_happened(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=10, cache_creation_5m=1_000, output_tokens=10)
        builder.add_compaction(
            pre_tokens=90_000, post_tokens=5_000, preserved_uuids=[], cumulative_dropped=150_000
        )
        builder.add_turn(input_tokens=10, cache_read=5_000, output_tokens=10)
        analysis = analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert any("before a compaction" in note for note in analysis.limitations)

    def test_a_version_spanning_session_says_so(self, tmp_path: Path) -> None:
        """FR-028 — a comparison crossing a version boundary must be identifiable."""
        builder = TranscriptBuilder(version="2.1.212")
        builder.add_turn(output_tokens=10)
        builder.add_turn(output_tokens=10, version="2.1.220")
        analysis = analyse_transcript(builder.write(tmp_path / "s.jsonl"), pricing=PRICING)
        assert any("spans Claude Code versions" in note for note in analysis.limitations)


class TestCauseProfiles:
    def test_read_repeatedly_and_read_once_are_distinguishable(self, tmp_path: Path) -> None:
        """The US2 question: same cost, opposite remedies, and the tool must tell them apart."""
        repeated = TranscriptBuilder()
        for _ in range(4):
            repeated.add_turn(cache_creation_5m=2_000, output_tokens=10, tool_use_ids=("t1",))
            repeated.add_tool_result(tool_use_id="t1", file_path="/repo/a.py", text="x\n" * 100)
        repeated.add_turn(cache_read=2_000, output_tokens=10)

        carried = TranscriptBuilder()
        carried.add_turn(cache_creation_5m=2_000, output_tokens=10, tool_use_ids=("t1",))
        carried.add_tool_result(tool_use_id="t1", file_path="/repo/a.py", text="x\n" * 100)
        for _ in range(6):
            carried.add_turn(cache_read=2_000, output_tokens=10)

        repeated_analysis = analyse_transcript(
            repeated.write(tmp_path / "repeated.jsonl"), pricing=PRICING
        )
        carried_analysis = analyse_transcript(
            carried.write(tmp_path / "carried.jsonl"), pricing=PRICING
        )

        repeated_item = next(iter(repeated_analysis.timeline.items))
        carried_item = next(iter(carried_analysis.timeline.items))
        assert repeated_analysis.timeline.load_count(repeated_item) == 4
        assert carried_analysis.timeline.load_count(carried_item) == 1
        assert carried_analysis.timeline.turns_resident(carried_item) > (
            repeated_analysis.timeline.turns_resident(repeated_item)
        )
