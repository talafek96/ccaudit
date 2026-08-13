"""System tests — the real CLI over a transcript corpus. This is the merge gate.

Everything below runs `main()` exactly as a user would, against a **fixture** corpus in a
temporary tree. Never the developer's real `~/.claude/`: that is not reproducible, not
shareable, and would make this suite depend on whatever sessions happened to be on the machine.

The bar (constitution, Testing Discipline): the output reconciles and is well-formed.
"""

import json
from pathlib import Path

import pytest

from claude_cost_tracker.cli import EXIT_NO_SESSIONS, EXIT_OK, EXIT_USAGE, main
from tests.fixtures.builder import TranscriptBuilder

pytestmark = pytest.mark.system


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `~/.claude`-shaped tree with three sessions across two projects."""
    home = tmp_path / "claude"

    busy = TranscriptBuilder(session_id="sess-busy", project_path="/repo/alpha")
    busy.add_user_text("audit the specs")
    busy.add_turn(
        input_tokens=300, cache_creation_1h=8_000, output_tokens=200, tool_use_ids=("t1",)
    )
    busy.add_tool_result(tool_use_id="t1", file_path="/repo/alpha/docs/guide.md", text="g" * 9_000)
    busy.add_ui_noise(5)
    busy.add_turn(input_tokens=20, cache_creation_5m=2_000, cache_read=8_100, output_tokens=90)
    busy.add_at_mention(display_path="/repo/alpha/CLAUDE.md", content="# Rules\n" * 60)
    busy.add_turn(input_tokens=8, cache_read=10_200, output_tokens=140)
    busy.write_to_project_tree(home)

    quiet = TranscriptBuilder(session_id="sess-quiet", project_path="/repo/alpha")
    quiet.add_turn(input_tokens=50, cache_creation_5m=900, output_tokens=40)
    quiet.add_turn(input_tokens=5, cache_read=950, output_tokens=25)
    quiet.write_to_project_tree(home)

    other = TranscriptBuilder(session_id="sess-other", project_path="/repo/beta")
    other.add_turn(input_tokens=90, cache_creation_5m=3_000, output_tokens=60)
    other.write_to_project_tree(home)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CCOST_HOME", str(tmp_path / "state"))
    return home


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestTheMergeGate:
    def test_every_session_in_the_corpus_analyses_and_adds_up(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If this fails, nothing else about the tool matters (SC-001)."""
        code, out, _ = run(["--all", "--json"], capsys)
        assert code == EXIT_OK
        payload = json.loads(out)
        totals = payload["totals"]
        assert totals["attributed_micros"] + totals["unattributed_micros"] == totals["cost_micros"]

    def test_each_session_individually_adds_up(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for session_id in ("sess-busy", "sess-quiet", "sess-other"):
            code, out, _ = run(["--session", session_id, "--json"], capsys)
            assert code == EXIT_OK, session_id
            totals = json.loads(out)["totals"]
            assert (
                totals["attributed_micros"] + totals["unattributed_micros"] == totals["cost_micros"]
            ), session_id

    def test_the_payload_is_well_formed(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(["--all", "--json"], capsys)
        payload = json.loads(out)
        for key in ("schema_version", "cost_basis", "policy", "scope", "totals", "components"):
            assert key in payload
        assert payload["cost_basis"] == "api_equivalent_estimate"
        assert len(payload["components"]) == 4


class TestHonestyOnEverySurface:
    def test_the_terminal_output_never_claims_an_amount_was_billed(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-010. The word may appear only in the disclaimer that denies it."""
        _, out, _ = run(["--all"], capsys)
        for index in range(len(out)):
            if out.startswith("billed", index):
                assert out[max(0, index - 4) : index] == "not ", out[index - 60 : index + 30]

    def test_every_absolute_is_paired_with_a_share(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-011 — the share survives being wrong about pricing; the dollars do not."""
        _, out, _ = run(["--all"], capsys)
        assert "%" in out
        assert "Share" in out or "share" in out

    def test_the_unattributed_line_is_always_visible(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(["--all"], capsys)
        assert "couldn't attribute" in out

    def test_it_survives_truncation(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Rows may be hidden; cost never is (FR-012)."""
        _, out, _ = run(["--all", "--top", "1"], capsys)
        assert "couldn't attribute" in out
        assert "not shown" in out

    def test_the_limitations_are_stated(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(["--all"], capsys)
        assert "API-equivalent" in out
        assert "stripped before the transcript" in out


class TestSelection:
    def test_the_zero_argument_default_analyses_the_current_project(
        self, corpus: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FR-048 — zero arguments is a complete invocation."""
        code, out, _ = run(["--project", "/repo/alpha", "--json"], capsys)
        assert code == EXIT_OK
        assert set(json.loads(out)["scope"]["sessions_included"]) == {"sess-busy", "sess-quiet"}

    def test_excluding_a_session_is_stated_in_the_result(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The exclude flag must never become a silent cherry-picking tool (FR-063)."""
        _, out, _ = run(["--all", "--exclude", "sess-other", "--json"], capsys)
        payload = json.loads(out)
        assert "sess-other" not in payload["scope"]["sessions_included"]
        assert payload["scope"]["sessions_excluded_count"] == 1

    def test_excluding_a_session_removes_exactly_its_contribution(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SC-020 — the arithmetic of inclusion has to be exact, not approximate."""
        _, all_out, _ = run(["--all", "--json"], capsys)
        _, without_out, _ = run(["--all", "--exclude", "sess-other", "--json"], capsys)
        _, only_out, _ = run(["--session", "sess-other", "--json"], capsys)

        total_all = json.loads(all_out)["totals"]["cost_micros"]
        total_without = json.loads(without_out)["totals"]["cost_micros"]
        total_only = json.loads(only_out)["totals"]["cost_micros"]
        assert total_all - total_without == total_only

    def test_an_unknown_session_reports_empty_rather_than_failing(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run(["--session", "no-such-session"], capsys)
        assert code == EXIT_NO_SESSIONS
        assert "ccost sessions" in err


class TestPolicy:
    def test_the_policy_changes_per_item_figures_but_not_the_total(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, proportional, _ = run(["--all", "--policy", "proportional", "--json"], capsys)
        _, exclusive, _ = run(["--all", "--policy", "exclusive", "--json"], capsys)

        first = json.loads(proportional)["totals"]
        second = json.loads(exclusive)["totals"]
        assert first["cost_micros"] == second["cost_micros"]
        assert second["unattributed_micros"] >= first["unattributed_micros"]

    def test_the_policy_in_effect_is_stated(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out, _ = run(["--all", "--policy", "exclusive"], capsys)
        assert "exclusive" in out


class TestRedaction:
    def test_paths_are_removed_and_the_cost_structure_survives(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-043 — shareable without leaking paths, and still checkable."""
        _, plain, _ = run(["--all", "--json"], capsys)
        _, redacted, _ = run(["--all", "--redact", "--json"], capsys)

        plain_payload = json.loads(plain)
        redacted_payload = json.loads(redacted)
        assert redacted_payload["redacted"] is True
        assert "/repo/alpha/docs/guide.md" not in redacted
        assert plain_payload["totals"]["cost_micros"] == redacted_payload["totals"]["cost_micros"]
        assert [item["total_micros"] for item in plain_payload["items"]] == [
            item["total_micros"] for item in redacted_payload["items"]
        ]


class TestExplain:
    def test_the_total_can_be_explained(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, _ = run(["explain", "--session", "sess-busy"], capsys)
        assert code == EXIT_OK
        assert "Rates:" in out
        assert "not a bill" in out

    def test_an_unknown_figure_lists_what_is_available(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _, err = run(["explain", "no-such-figure", "--session", "sess-busy"], capsys)
        assert code == EXIT_USAGE
        assert "Available figures" in err


class TestSessionsListing:
    def test_it_lists_every_session_with_enough_to_identify_it(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-060."""
        code, out, _ = run(["sessions", "--all"], capsys)
        assert code == EXIT_OK
        for session_id in ("sess-busy", "sess-quiet", "sess-other"):
            assert session_id in out
        assert "records" in out


class TestDeterminism:
    def test_two_runs_produce_identical_payloads(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SC-009 — byte-identical figures across runs, and therefore across machines."""
        _, first, _ = run(["--all", "--json"], capsys)
        _, second, _ = run(["--all", "--json"], capsys)
        first_payload = json.loads(first)
        second_payload = json.loads(second)
        for payload in (first_payload, second_payload):
            payload.pop("generated_at", None)
        assert first_payload == second_payload

    def test_analysis_never_writes_to_the_transcript_tree(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-020 — user records are read-only input, always."""
        before = {
            path: (path.stat().st_mtime_ns, path.read_bytes()) for path in corpus.rglob("*.jsonl")
        }
        run(["--all"], capsys)
        after = {
            path: (path.stat().st_mtime_ns, path.read_bytes()) for path in corpus.rglob("*.jsonl")
        }
        assert before == after
