"""Contract on the interactive surface — the parts that can be pinned without a socket.

The promises fenced here: a selection is read exactly as given or refused, the page is the report
document with controls wrapped around it (one renderer, not two), the terminal equivalent of the
current view is stated, and a payload that does not add up is never rendered.
"""

import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import pytest

from ccaudit.analyse import SessionAnalysis
from ccaudit.ingest.discover import SessionRef, fingerprint_transcript
from ccaudit.model.reconcile import ReconciliationError
from ccaudit.render.data import build_report_data
from ccaudit.render.report import REPORT_TITLE
from ccaudit.render.serve import (
    Selection,
    UiHttpServer,
    UiServer,
    payload_json,
    render_ui_html,
    selection_from_query,
    terminal_command,
)
from tests.fixtures.builder import TranscriptBuilder
from tests.unit.test_report_data import FIXED_TIME, analyse, busy_session


@pytest.fixture
def analysis(tmp_path: Path) -> SessionAnalysis:
    return analyse(busy_session(), tmp_path)


@pytest.fixture
def payload(analysis: SessionAnalysis) -> dict:
    return build_report_data([analysis], generated_at=FIXED_TIME)


@pytest.fixture
def sessions(tmp_path: Path) -> list[SessionRef]:
    """Two session references, built the way discovery builds them."""
    refs = []
    for name in ("sess-one", "sess-two"):
        path = TranscriptBuilder(session_id=name).write(tmp_path / f"{name}.jsonl")
        refs.append(
            SessionRef(
                session_id=name,
                path=path,
                project_dir="-repo-alpha",
                project_path=Path("/repo/alpha"),
                fingerprint=fingerprint_transcript(path),
                modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                in_progress=False,
            )
        )
    return refs


class TestTheSelectionIsReadExactly:
    def test_an_empty_query_keeps_the_selection_the_command_started_with(self) -> None:
        default = Selection(session_ids=("a",), group_by="ext", redact=True)
        assert selection_from_query({}, default=default, known=["a"]) == default

    def test_several_sessions_are_all_carried(self) -> None:
        chosen = selection_from_query(
            {"session": ["a", "b"]}, default=Selection(session_ids=("a",)), known=["a", "b"]
        )
        assert chosen.session_ids == ("a", "b")

    def test_an_unknown_session_is_refused_rather_than_ignored(self) -> None:
        with pytest.raises(ValueError, match="unknown session"):
            selection_from_query(
                {"session": ["ghost"]}, default=Selection(session_ids=("a",)), known=["a"]
            )

    def test_an_unknown_grouping_is_refused_rather_than_defaulted(self) -> None:
        """A silent fallback would show one grouping while the URL claimed another."""
        with pytest.raises(ValueError, match="unknown grouping"):
            selection_from_query(
                {"by": ["colour"]}, default=Selection(session_ids=("a",)), known=["a"]
            )

    def test_redaction_is_off_unless_asked_for(self) -> None:
        """An unchecked box submits nothing, so absence in a real query means off, not unset."""
        default = Selection(session_ids=("a",), redact=True)
        assert selection_from_query({}, default=default, known=["a"]).redact is True
        assert selection_from_query({"by": ["ext"]}, default=default, known=["a"]).redact is False
        assert selection_from_query({"redact": ["1"]}, default=default, known=["a"]).redact is True


class TestNothingIsBrowserOnly:
    def test_the_page_states_the_terminal_command_for_what_is_on_screen(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        """FR-074, made visible to the reader rather than merely true."""
        selection = Selection(session_ids=("sess-one",), group_by="ext", redact=True)
        assert terminal_command(selection) == "ccaudit --session sess-one --by ext --redact"
        html = render_ui_html(payload, sessions=sessions, selection=selection)
        assert "ccaudit --session sess-one --by ext --redact" in html

    def test_the_json_matches_the_cli_serialisation_byte_for_byte(self, payload: dict) -> None:
        assert payload_json(payload) == json.dumps(payload, indent=2, sort_keys=False) + "\n"


class TestOneRendererTwoShells:
    def test_the_page_is_the_report_document_with_controls_around_it(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        html = render_ui_html(
            payload, sessions=sessions, selection=Selection(session_ids=("sess-one",))
        )
        assert '<script type="application/json" id="ccaudit-data">' in html
        assert f"<title>{escape(REPORT_TITLE)}</title>" in html
        # The shell, not its class name: this pins that the report document is *wrapped* by
        # controls, which is the property. A previous version asserted `class="ui-controls"`
        # and failed when the panel was restyled, which tested the stylesheet, not the shell.
        assert '<nav class="ui"' in html
        assert "</nav>" in html
        assert html.index('<nav class="ui"') < html.index("<main>")

    def test_every_session_is_offered_and_the_current_one_is_marked(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        html = render_ui_html(
            payload, sessions=sessions, selection=Selection(session_ids=("sess-two",))
        )
        # Offered, and its state is right. The trailing-space form this used to assert was a
        # detail of one line of markup — the invariant is that every session appears with a
        # checkbox, and that the selected one is the one checked.
        assert 'value="sess-one"' in html
        assert 'value="sess-two" checked' in html

    def test_the_page_references_no_external_url(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        """FR-073 — the interactive surface fetches nothing from off the machine."""
        html = render_ui_html(
            payload, sessions=sessions, selection=Selection(session_ids=("sess-one",))
        )
        for scheme in ("http://", "https://", "//cdn", 'src="//'):
            assert scheme not in html

    def test_it_says_it_is_the_exploring_surface_not_the_shareable_one(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        """FR-075 — the two surfaces must not be mistaken for each other."""
        html = render_ui_html(
            payload, sessions=sessions, selection=Selection(session_ids=("sess-one",))
        )
        assert "ccaudit report" in html
        assert "not the shareable artifact" in html


class TestItRefusesWhatDoesNotAddUp:
    def test_a_payload_whose_parts_contradict_its_total_is_never_served(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        broken = json.loads(json.dumps(payload))
        broken["totals"]["unattributed_micros"] += 1

        server = UiHttpServer(lambda _selection: broken, sessions, Selection(("sess-one",)))
        try:
            with pytest.raises(ReconciliationError):
                server.payload_for(Selection(("sess-one",)))
        finally:
            server.server_close()

    def test_a_sound_payload_passes_through_unchanged(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        server = UiHttpServer(lambda _selection: payload, sessions, Selection(("sess-one",)))
        try:
            assert server.payload_for(Selection(("sess-one",))) is payload
        finally:
            server.server_close()


class TestTheLifetimeIsBounded:
    def test_it_binds_loopback_on_an_os_assigned_port(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        server = UiServer(lambda _selection: payload, sessions, Selection(("sess-one",)))
        try:
            assert server.port > 0
            assert server.url.startswith("http://127.0.0.1:")
        finally:
            server.close()

    def test_closing_before_it_ever_served_does_not_hang(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        """`close` is called from every exit path, including ones that never started serving."""
        server = UiServer(lambda _selection: payload, sessions, Selection(("sess-one",)))
        server.close()
        server.close()
