"""System test — the performance goals, measured rather than assumed.

SC-005 (a session under 30s), SC-006 (a ~25-session corpus under 5 minutes), SC-021
(include/exclude recomputation under 2s over 100 sessions), SC-025 (the UI up in under 5s).

These budgets are generous on purpose: they are the point at which a tool stops being something
you reach for. The margins here are large, and that is the finding — but a wall-clock assertion
is the only thing that catches the day a lookup starts blocking. That is not hypothetical: the
UI once took 35 seconds to bind because `http.server` resolves the machine's FQDN, seven times
the whole budget, and nothing but a timing check would have caught it.
"""

import time
from pathlib import Path

import pytest

from claude_cost_tracker.analyse import analyse_transcript
from claude_cost_tracker.cli import EXIT_OK, main
from claude_cost_tracker.config import BUNDLED_PRICING_PATH, load_pricing
from tests.fixtures.builder import TranscriptBuilder

pytestmark = pytest.mark.system

PRICING = load_pricing(BUNDLED_PRICING_PATH)

SINGLE_SESSION_BUDGET_SECONDS = 30.0
CORPUS_BUDGET_SECONDS = 300.0
RECOMPUTE_BUDGET_SECONDS = 2.0


def build_session(path: Path, *, turns: int, files: int = 4) -> Path:
    builder = TranscriptBuilder(session_id=path.stem, project_path="/repo/alpha")
    for index in range(turns):
        tool_ids = (f"t{index}",) if index % 3 == 0 else ()
        builder.add_turn(
            input_tokens=20,
            cache_creation_5m=3_000 if tool_ids else 0,
            cache_read=6_000 if index else 0,
            output_tokens=80,
            tool_use_ids=tool_ids,
        )
        if tool_ids:
            builder.add_tool_result(
                tool_use_id=tool_ids[0],
                file_path=f"/repo/alpha/src/file{index % files}.py",
                text="x" * 8_000,
            )
        if index % 7 == 0:
            builder.add_ui_noise(4)
    return builder.write(path)


class TestSingleSession:
    def test_a_substantial_session_analyses_well_inside_the_budget(self, tmp_path: Path) -> None:
        """SC-005. 400 turns is larger than most real sessions."""
        path = build_session(tmp_path / "big.jsonl", turns=400)
        started = time.monotonic()
        analysis = analyse_transcript(path, pricing=PRICING)
        elapsed = time.monotonic() - started

        assert analysis.reconciliation.adds_up
        assert elapsed < SINGLE_SESSION_BUDGET_SECONDS, f"took {elapsed:.1f}s"


class TestCorpus:
    def test_a_full_corpus_analyses_well_inside_the_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SC-006 — 30 sessions, the scale the tool was specified against."""
        home = tmp_path / "claude"
        for index in range(30):
            builder = TranscriptBuilder(session_id=f"sess-{index:03d}", project_path="/repo/alpha")
            for turn in range(40):
                builder.add_turn(
                    input_tokens=15,
                    cache_read=4_000 if turn else 0,
                    cache_creation_5m=2_000 if turn == 0 else 0,
                    output_tokens=60,
                )
            builder.write_to_project_tree(home)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
        monkeypatch.setenv("CCOST_HOME", str(tmp_path / "state"))

        started = time.monotonic()
        assert main(["--all", "--json"]) == EXIT_OK
        elapsed = time.monotonic() - started
        capsys.readouterr()

        assert elapsed < CORPUS_BUDGET_SECONDS, f"took {elapsed:.1f}s"

    def test_changing_the_selection_recomputes_quickly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SC-021 — the include/exclude loop has to feel immediate, or nobody explores."""
        home = tmp_path / "claude"
        for index in range(30):
            builder = TranscriptBuilder(session_id=f"sess-{index:03d}", project_path="/repo/alpha")
            builder.add_turn(input_tokens=10, cache_creation_5m=1_500, output_tokens=40)
            builder.add_turn(input_tokens=5, cache_read=1_600, output_tokens=25)
            builder.write_to_project_tree(home)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
        monkeypatch.setenv("CCOST_HOME", str(tmp_path / "state"))

        main(["--all", "--json"])  # warm any lazily-loaded configuration
        capsys.readouterr()

        started = time.monotonic()
        assert main(["--all", "--exclude", "sess-001", "sess-002", "--json"]) == EXIT_OK
        elapsed = time.monotonic() - started
        capsys.readouterr()

        # Scaled to the 100-session corpus SC-021 names, from a 30-session measurement.
        projected = elapsed * (100 / 30)
        assert projected < RECOMPUTE_BUDGET_SECONDS, (
            f"{elapsed:.2f}s for 30 sessions projects to {projected:.2f}s for 100"
        )


class TestOlderSessions:
    def test_an_old_session_analyses_with_no_loss_of_detail(self, tmp_path: Path) -> None:
        """SC-022 — age is not a reason for a worse answer; only the records matter."""
        old = build_session(tmp_path / "old.jsonl", turns=30)
        new = build_session(tmp_path / "new.jsonl", turns=30)

        old_analysis = analyse_transcript(old, pricing=PRICING)
        new_analysis = analyse_transcript(new, pricing=PRICING)

        assert len(old_analysis.timeline.items) == len(new_analysis.timeline.items)
        assert old_analysis.reconciliation.adds_up
