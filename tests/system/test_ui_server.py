"""System tests for the interactive interface — a real socket, over a fixture corpus.

The bar here is not "it renders". It is the promise in FR-073 and SC-025 that this is a command
and not a service: it is reachable only from this machine, it starts immediately, it shows exactly
what the terminal would show, and when it is stopped there is nothing left listening.

Every server in this file is created inside a context manager on an OS-assigned port, so a failing
assertion cannot leak a listening socket into the rest of the run.
"""

import json
import re
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from claude_cost_tracker.analyse import analyse_transcript
from claude_cost_tracker.cli import EXIT_OK, main
from claude_cost_tracker.config import load_pricing
from claude_cost_tracker.ingest.discover import SessionRef, discover_sessions
from claude_cost_tracker.render.data import (
    ANALYSED_SESSION_METRICS,
    build_report_data,
    session_facts,
)
from claude_cost_tracker.render.serve import Selection, UiServer
from tests.fixtures.builder import TranscriptBuilder

pytestmark = pytest.mark.system

FIXED_TIME = "2026-08-11T12:00:00+00:00"


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two sessions with materially different costs, so a switch is visible in the figures."""
    home = tmp_path / "claude"

    busy = TranscriptBuilder(session_id="sess-busy", project_path="/repo/alpha")
    busy.add_user_text("audit the specs")
    busy.add_turn(
        input_tokens=300, cache_creation_1h=8_000, output_tokens=200, tool_use_ids=("t1",)
    )
    busy.add_tool_result(tool_use_id="t1", file_path="/repo/alpha/docs/guide.md", text="g" * 9_000)
    busy.add_turn(input_tokens=20, cache_creation_5m=2_000, cache_read=8_100, output_tokens=90)
    busy.write_to_project_tree(home)

    quiet = TranscriptBuilder(session_id="sess-quiet", project_path="/repo/alpha")
    quiet.add_turn(input_tokens=50, cache_creation_5m=900, output_tokens=40)
    quiet.write_to_project_tree(home)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CCOST_HOME", str(tmp_path / "state"))
    return home


def build_payload(selection: Selection) -> dict:
    """The provider the CLI wires in: analyse the selection, build the payload, nothing else."""
    wanted = set(selection.session_ids)
    refs = [ref for ref in discover_sessions() if ref.session_id in wanted]
    pricing = load_pricing()
    analyses = [
        analyse_transcript(
            ref.path,
            pricing=pricing,
            project_path=str(ref.project_path) if ref.project_path else None,
            provisional=ref.in_progress,
        )
        for ref in refs
    ]
    return build_report_data(
        analyses, redact=selection.redact, group_by=selection.group_by, generated_at=FIXED_TIME
    )


@pytest.fixture
def sessions(corpus: Path) -> list[SessionRef]:
    return discover_sessions()


@pytest.fixture
def server(sessions: list[SessionRef]) -> Iterator[UiServer]:
    with UiServer(build_payload, sessions, Selection(("sess-busy",))) as running:
        yield running


def get(server: UiServer, path: str) -> str:
    with urlopen(server.url.rstrip("/") + path, timeout=5) as response:
        return str(response.read().decode("utf-8"))


def this_machines_external_address() -> str | None:
    """This machine's own non-loopback address, without sending a packet or resolving a name.

    Connecting a UDP socket only picks a route, so it works offline and on a machine whose
    hostname does not resolve — which is exactly the machine this test suite has to run on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            # TEST-NET-1 (RFC 5737): reserved for documentation, routed nowhere.
            probe.connect(("192.0.2.1", 9))
        except OSError:
            return None
        address = str(probe.getsockname()[0])
    return None if address.startswith("127.") else address


def is_listening(port: int, host: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) == 0


class TestItServesOnlyThisMachine:
    def test_it_binds_loopback_and_nothing_else(self, server: UiServer) -> None:
        """FR-073 — unreachable from another machine, by binding rather than by filtering."""
        assert server.url.startswith("http://127.0.0.1:")
        assert is_listening(server.port, "127.0.0.1")

        external = this_machines_external_address()
        if external is None:
            pytest.skip("this machine has no non-loopback address to test against")
        assert not is_listening(server.port, external)

    def test_no_response_points_at_anything_off_this_machine(self, server: UiServer) -> None:
        for path in ("/", "/data?session=sess-busy", "/sessions"):
            body = get(server, path)
            assert "https://" not in body
            assert "http://" not in body


class TestItStartsImmediately:
    def test_bound_and_answering_well_inside_the_budget(self, sessions: list[SessionRef]) -> None:
        """SC-025 allows five seconds from the command to a served page."""
        started = time.monotonic()
        with UiServer(build_payload, sessions, Selection(("sess-busy",))) as running:
            get(running, "/")
            elapsed = time.monotonic() - started
        assert elapsed < 5.0


class TestNothingIsLeftRunning:
    def test_shutdown_stops_it_and_frees_the_port(self, sessions: list[SessionRef]) -> None:
        """FR-073, SC-025 — the whole reason this is a command and not a service."""
        with UiServer(build_payload, sessions, Selection(("sess-busy",))) as running:
            port = running.port
            request = Request(running.url + "shutdown", data=b"", method="POST")
            with urlopen(request, timeout=5) as response:
                assert "Stopped" in response.read().decode("utf-8")
            # Stopping releases the port on its own: `run` closes the socket as it unwinds, so
            # nothing outside the server has to clean up after it.
            deadline = time.monotonic() + 5
            while is_listening(port, "127.0.0.1"):
                if time.monotonic() > deadline:
                    raise AssertionError("the port was still accepting connections after /shutdown")
                time.sleep(0.05)

        assert not is_listening(port, "127.0.0.1")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rebind:
            rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rebind.bind(("127.0.0.1", port))

    def test_leaving_the_context_stops_it_even_when_something_failed(
        self, sessions: list[SessionRef]
    ) -> None:
        port = 0
        with (
            pytest.raises(RuntimeError),
            UiServer(build_payload, sessions, Selection(("sess-busy",))) as running,
        ):
            port = running.port
            raise RuntimeError("something went wrong while the user was exploring")
        assert not is_listening(port, "127.0.0.1")


class TestTheBrowserShowsWhatTheTerminalShows:
    def test_the_served_json_is_what_the_cli_prints_for_the_same_selection(
        self, server: UiServer, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-074 made checkable: the same selection, the same bytes.

        Compared as text, not as parsed objects — key order, indentation, and number formatting
        are part of what a reader diffs when checking one surface against the other. Only the
        timestamp is normalised, because the two runs happened at different moments.
        """
        assert main(["--session", "sess-busy", "--by", "ext", "--json"]) == EXIT_OK
        from_terminal = capsys.readouterr().out
        from_browser = get(server, "/data?session=sess-busy&by=ext")

        stamp = re.compile(r'"generated_at": "[^"]*"')
        assert stamp.sub("", from_browser) == stamp.sub("", from_terminal)

    def test_switching_sessions_returns_the_other_session_figures(self, server: UiServer) -> None:
        busy = json.loads(get(server, "/data?session=sess-busy"))
        quiet = json.loads(get(server, "/data?session=sess-quiet"))
        both = json.loads(get(server, "/data?session=sess-busy&session=sess-quiet"))

        assert quiet["scope"]["sessions_included"] == ["sess-quiet"]
        assert busy["totals"]["cost_micros"] != quiet["totals"]["cost_micros"]
        assert (
            both["totals"]["cost_micros"]
            == busy["totals"]["cost_micros"] + quiet["totals"]["cost_micros"]
        )

    def test_the_page_carries_the_honesty_wording_the_terminal_carries(
        self, server: UiServer
    ) -> None:
        page = get(server, "/")
        assert "API-equivalent" in page
        assert "not billed amounts" in page
        assert "couldn&#x27;t attribute" in page or "couldn't attribute" in page

    def test_a_selection_it_cannot_honour_is_refused_rather_than_guessed(
        self, server: UiServer
    ) -> None:
        for path in ("/data?session=no-such-session", "/data?by=colour"):
            with pytest.raises(HTTPError) as failure:
                get(server, path)
            assert failure.value.code == 400

    def test_an_unknown_path_is_a_plain_404(self, server: UiServer) -> None:
        with pytest.raises(HTTPError) as failure:
            get(server, "/admin")
        assert failure.value.code == 404


class TestItRefusesABreakdownThatDoesNotAddUp:
    def test_a_non_reconciling_payload_is_never_served(self, sessions: list[SessionRef]) -> None:
        def broken(selection: Selection) -> dict:
            payload = build_payload(selection)
            payload["totals"]["unattributed_micros"] += 1
            return payload

        with UiServer(broken, sessions, Selection(("sess-busy",))) as running:
            with pytest.raises(HTTPError) as failure:
                get(running, "/data?session=sess-busy")
            assert failure.value.code == 500
            assert "does not add up" in failure.value.read().decode("utf-8")


class TestTheSessionPickerCanBeRanked:
    """The measured columns arrive per session, after the page. Over a real socket, because the
    contract is an HTTP one: the picker asks for one session at a time and fills cells in.
    """

    @pytest.fixture
    def measured(self, sessions: list[SessionRef]) -> Iterator[UiServer]:
        def facts(session_id: str) -> dict[str, int]:
            reference = next(ref for ref in sessions if ref.session_id == session_id)
            analysis = analyse_transcript(
                reference.path,
                pricing=load_pricing(),
                project_path=str(reference.project_path) if reference.project_path else None,
                provisional=reference.in_progress,
            )
            return session_facts(analysis)

        with UiServer(build_payload, sessions, Selection(("sess-busy",)), facts=facts) as running:
            yield running

    def test_it_answers_for_one_session(self, measured: UiServer) -> None:
        facts = json.loads(get(measured, "/facts?session=sess-busy"))

        for key in ANALYSED_SESSION_METRICS:
            assert key in facts, key
        assert facts["cost_micros"] > 0

    def test_the_cost_it_reports_is_the_one_the_page_shows(self, measured: UiServer) -> None:
        """The picker and the breakdown it selects for must not disagree about a session."""
        facts = json.loads(get(measured, "/facts?session=sess-busy"))
        payload = json.loads(get(measured, "/data?session=sess-busy"))

        assert facts["cost_micros"] == payload["totals"]["cost_micros"]

    def test_an_unknown_session_is_refused(self, measured: UiServer) -> None:
        """Answering with zeroes would put a wrong figure in a column people rank by."""
        with pytest.raises(HTTPError) as exc:
            get(measured, "/facts?session=not-a-session")
        assert exc.value.code == 404

    def test_the_page_itself_does_not_wait_for_any_of_it(self, sessions: list[SessionRef]) -> None:
        """SC-025: the picker is on screen before a single session has been measured."""

        def facts(session_id: str) -> dict[str, int]:
            raise AssertionError("the page must not need a measured column to render")

        with UiServer(build_payload, sessions, Selection(("sess-busy",)), facts=facts) as running:
            assert 'id="ui-session-table"' in get(running, "/")
