"""System test — multi-session accumulation and exclusion (US6, SC-020).

One session is an anecdote. The manager-facing argument and the "which files are chronically
expensive" question both need accumulation, and both collapse if the arithmetic of inclusion
is approximate: the combined figures must equal the sum of the per-session ones, and excluding
a session must subtract *exactly* its contribution, not roughly.

The exclusion also has to be part of the answer rather than a hidden input. A flag that
silently drops sessions is a cherry-picking tool, which is the opposite of what this is for.
"""

import json
from pathlib import Path

import pytest

from ccaudit.cli import EXIT_NO_SESSIONS, EXIT_OK, main
from tests.fixtures.builder import TranscriptBuilder

pytestmark = pytest.mark.system

SESSIONS = ("alpha-1", "alpha-2", "beta-1")


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three sessions across two projects, sharing one file so accumulation has something
    to accumulate."""
    home = tmp_path / "claude"
    for index, session_id in enumerate(SESSIONS):
        project = "/repo/alpha" if session_id.startswith("alpha") else "/repo/beta"
        builder = TranscriptBuilder(session_id=session_id, project_path=project)
        builder.add_turn(
            input_tokens=40 + index,
            cache_creation_5m=3_000 + index * 500,
            output_tokens=70,
            tool_use_ids=("t1",),
        )
        builder.add_tool_result(
            tool_use_id="t1", file_path="/repo/shared/common.md", text="c" * (6_000 + index * 400)
        )
        builder.add_turn(input_tokens=8, cache_read=3_200 + index * 500, output_tokens=35)
        builder.add_turn(input_tokens=4, cache_read=3_200 + index * 500, output_tokens=20)
        builder.write_to_project_tree(home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CCAUDIT_HOME", str(tmp_path / "state"))
    return home


def payload(capsys: pytest.CaptureFixture[str], *args: str) -> dict:
    assert main([*args, "--json"]) == EXIT_OK
    return json.loads(capsys.readouterr().out)


class TestAccumulation:
    def test_the_combined_total_is_the_sum_of_the_per_session_totals(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SC-020, the half that makes a corpus figure mean anything."""
        combined = payload(capsys, "--all")["totals"]["cost_micros"]
        individually = sum(
            payload(capsys, "--session", session_id)["totals"]["cost_micros"]
            for session_id in SESSIONS
        )
        assert combined == individually

    def test_a_shared_file_accumulates_across_sessions(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        combined = payload(capsys, "--all")
        shared = next(item for item in combined["items"] if item["display"].endswith("common.md"))
        per_session = sum(entry["total_micros"] for entry in shared["per_session"])
        assert per_session == shared["total_micros"]
        assert len(shared["per_session"]) == len(SESSIONS)

    def test_a_multi_session_figure_decomposes_back_to_its_sessions(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-064, FR-065 — the accumulated number must not be a dead end."""
        combined = payload(capsys, "--all")
        for item in combined["items"]:
            assert item["per_session"]
            assert (
                sum(entry["total_micros"] for entry in item["per_session"])
                == (item["total_micros"])
            )


class TestExclusion:
    def test_excluding_a_session_subtracts_exactly_its_contribution(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SC-020 — exactly, not approximately. A near-miss here is a wrong argument."""
        everything = payload(capsys, "--all")["totals"]["cost_micros"]
        without = payload(capsys, "--all", "--exclude", "beta-1")["totals"]["cost_micros"]
        only = payload(capsys, "--session", "beta-1")["totals"]["cost_micros"]
        assert everything - without == only

    def test_the_result_states_what_was_excluded(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-063 — the exclusion is part of the result, never a hidden input."""
        data = payload(capsys, "--all", "--exclude", "beta-1")
        assert data["scope"]["sessions_excluded_count"] == 1
        assert "beta-1" not in data["scope"]["sessions_included"]

    def test_the_terminal_says_it_too(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--all", "--exclude", "beta-1"]) == EXIT_OK
        assert "excluded" in capsys.readouterr().out.lower()

    def test_excluding_everything_is_an_empty_result_not_a_crash(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--all", "--exclude", *SESSIONS]) == EXIT_NO_SESSIONS


class TestProjectSelection:
    def test_a_project_selection_takes_only_that_project(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = payload(capsys, "--project", "/repo/alpha")
        assert set(data["scope"]["sessions_included"]) == {"alpha-1", "alpha-2"}

    def test_every_selection_still_adds_up(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for args in (
            ["--all"],
            ["--project", "/repo/alpha"],
            ["--all", "--exclude", "alpha-1"],
            ["--all", "--last", "2"],
        ):
            totals = payload(capsys, *args)["totals"]
            assert (
                totals["attributed_micros"] + totals["unattributed_micros"] == totals["cost_micros"]
            ), args
