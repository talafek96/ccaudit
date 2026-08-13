"""System test — analysis never touches the network, and never needs a credential.

FR-029, FR-030, SC-011. Session transcripts contain file paths, shell commands, and source
code from whatever sessions produced them; on a work machine that may be proprietary material.
The guarantee that none of it leaves the machine is only worth what it can be checked against,
so this test makes it checkable: it removes the ability to open a socket at all and asserts the
tool still produces a complete result.

The one deliberate exception is `ccost pricing refresh`, which fetches a public rate table
and carries no session data in either direction. It is asserted here to be the *only* thing
that reaches out.
"""

import socket
from pathlib import Path

import pytest

from claude_cost_tracker.cli import EXIT_OK, main
from tests.fixtures.builder import TranscriptBuilder

pytestmark = pytest.mark.system


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outbound connection raise.

    Patched at `socket.socket` so it catches every route out — urllib, http.client, a raw
    socket, anything a future dependency might reach for. A test that only patched `urlopen`
    would pass while a new library quietly phoned home.
    """

    class Blocked(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError(
                "analysis attempted to open a socket. Nothing about a user's sessions may "
                "leave the machine (FR-030)."
            )

    monkeypatch.setattr(socket, "socket", Blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked_connection)


def _blocked_connection(*args: object, **kwargs: object) -> None:
    raise AssertionError("analysis attempted to open a connection (FR-030)")


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude"
    builder = TranscriptBuilder(session_id="offline-1", project_path="/repo/alpha")
    builder.add_turn(
        input_tokens=200, cache_creation_5m=6_000, output_tokens=90, tool_use_ids=("t1",)
    )
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/alpha/a.py", text="x" * 6_000)
    builder.add_turn(input_tokens=10, cache_read=6_200, output_tokens=40)
    builder.write_to_project_tree(home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CCOST_HOME", str(tmp_path / "state"))
    return home


class TestAnalysisIsOffline:
    def test_a_full_analysis_completes_with_no_network_available(
        self, corpus: Path, no_network: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--all"]) == EXIT_OK
        assert "Total" in capsys.readouterr().out

    def test_json_output_completes_offline(
        self, corpus: Path, no_network: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--all", "--json"]) == EXIT_OK
        assert '"cost_basis"' in capsys.readouterr().out

    def test_the_report_is_produced_offline(
        self, corpus: Path, no_network: None, tmp_path: Path
    ) -> None:
        out = tmp_path / "report.html"
        assert main(["report", "--all", "--out", str(out)]) == EXIT_OK
        assert out.is_file()

    def test_explain_works_offline(
        self, corpus: Path, no_network: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["explain", "--all"]) == EXIT_OK

    def test_the_session_listing_works_offline(self, corpus: Path, no_network: None) -> None:
        assert main(["sessions", "--all"]) == EXIT_OK

    def test_pricing_show_works_offline(self, corpus: Path, no_network: None) -> None:
        """Reading the rate table is local; only refreshing it is not."""
        assert main(["pricing", "show"]) == EXIT_OK


class TestNoCredentialIsRequired:
    def test_it_runs_with_no_environment_configured(
        self, corpus: Path, no_network: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SC-011 — a Claude Code install is the only prerequisite."""
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CCOST_PRICING",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(name, raising=False)
        assert main(["--all"]) == EXIT_OK

    def test_no_source_file_reads_an_api_key(self) -> None:
        """A credential this tool never needs is one it can never leak."""
        source_root = Path(__file__).resolve().parents[2] / "src" / "claude_cost_tracker"
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "ANTHROPIC_API_KEY" not in text, path
            assert "api_key" not in text.lower(), path


class TestRefreshIsTheOnlyWayOut:
    def test_refresh_is_the_only_module_that_can_open_a_socket(self) -> None:
        """Structural, not behavioural: nothing else even imports a network client."""
        source_root = Path(__file__).resolve().parents[2] / "src" / "claude_cost_tracker"
        offenders = []
        for path in source_root.rglob("*.py"):
            if path.name == "refresh.py" or "serve" in path.name:
                continue  # the refresh command, and the loopback-only UI server
            text = path.read_text(encoding="utf-8")
            for marker in ("urllib.request", "http.client", "import requests", "httpx"):
                if marker in text:
                    offenders.append(f"{path.name}: {marker}")
        assert offenders == [], f"unexpected network capability: {offenders}"

    def test_refresh_from_a_local_file_needs_no_network(
        self, corpus: Path, no_network: None, tmp_path: Path
    ) -> None:
        """The air-gapped path has to work with the socket layer removed entirely."""
        source = tmp_path / "rates.json"
        source.write_text(
            '{"claude-opus-5": {"litellm_provider": "anthropic", '
            '"input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05}}',
            encoding="utf-8",
        )
        assert main(["pricing", "refresh", "--from", str(source)]) == EXIT_OK
