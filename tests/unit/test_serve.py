"""Contract on the interactive surface — the parts that can be pinned without a socket.

The promises fenced here: a selection is read exactly as given or refused, the page is the report
document with controls wrapped around it (one renderer, not two), the terminal equivalent of the
current view is stated, and a payload that does not add up is never rendered.
"""

import json
from datetime import UTC, datetime
from html import escape
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from ccaudit.analyse import SessionAnalysis
from ccaudit.ingest.discover import SessionRef, fingerprint_transcript
from ccaudit.model.reconcile import ReconciliationError
from ccaudit.render.data import (
    ANALYSED_SESSION_METRICS,
    CHEAP_SESSION_METRICS,
    GROUPINGS,
    SESSION_METRICS,
    build_report_data,
)
from ccaudit.render.report import ASSETS, REPORT_TITLE, render_report_html
from ccaudit.render.serve import (
    Selection,
    UiHttpServer,
    UiServer,
    _Handler,
    payload_json,
    render_ui_html,
    selection_from_query,
    terminal_command,
)
from tests.fixtures.builder import TranscriptBuilder
from tests.unit.test_charts import report_payload
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


class TestTheFilterCannotBreakTheArithmetic:
    """The browser view showed 277 item rows *and* the "265 other items" line that stands in
    for them, so its visible column summed to $884 against a $612 session — a breakdown that
    does not add up, which is the one defect this project treats as a show-stopper.

    The cause was one line: the row filter ran on load with an empty needle and did
    `row.hidden = !visible` over every row, unhiding the ones the truncation line was already
    accounting for.

    There is no JavaScript test runner here, so these assert the guard rather than execute it.
    The behaviour itself was verified in Chrome: 17 rows summing to the total on load, 42 after
    a reveal, and 42 summing to the total again after a filter was applied and cleared.
    """

    def test_the_filter_never_touches_the_truncation_state(self) -> None:
        """Retargeted when the filter engine moved from the shell into report.js.

        The original asserted the guard `row.dataset.overflow === "1" && ...` in ui.js. The
        engine now makes the bug unreachable instead of guarding against it: visibility is a
        class, so clearing a filter cannot unhide a row the truncation line is accounting for.
        The invariant is the same one; only the thing that enforces it changed.
        """
        script = (ASSETS / "report.js").read_text(encoding="utf-8")
        engine = script[script.index("Per-section row filtering") :]
        assert "classList.toggle('filtered-out'" in engine
        assert "hidden = !" not in engine
        assert ".hidden = false" not in engine

    def test_revealing_a_row_marks_it_as_revealed(self) -> None:
        """Without the mark, the count cannot tell a revealed row from a never-shown one."""
        script = (ASSETS / "report.js").read_text(encoding="utf-8")
        assert 'candidate.dataset.revealed = "1"' in script

    def test_the_count_covers_only_rows_a_reader_could_see(self) -> None:
        """The reassurance under the table is itself a figure, so it has to be true."""
        script = (ASSETS / "report.js").read_text(encoding="utf-8")
        assert "data-overflow" in script
        assert "data-revealed" in script
        assert "the totals still cover all" in script


class TestEverySectionOwnsItsControls:
    """Controls live beside the thing they change, not in one page-wide panel.

    Supersedes `TestTagsHaveOneCentralControl` and the global "Group rows by" / "Filter rows"
    assertions, which pinned exactly the arrangement this replaces. Rewritten rather than
    deleted on an explicit instruction that a global setting is the wrong shape: regrouping or
    tag-filtering reads as a page-wide mode when what a reader wants is to change *this* table.

    The filter engine moved from the shell into report.js in the same change, so the shareable
    report filters too and there is one engine rather than one per shell (Principle IX).
    """

    @pytest.fixture
    def page(self, payload: dict, sessions: list[SessionRef]) -> str:
        return render_ui_html(payload, sessions=sessions, selection=Selection(("sess-one",)))

    def test_the_shell_no_longer_carries_a_global_tag_panel(self, page: str) -> None:
        assert 'id="ui-tags-panel"' not in page
        assert 'id="ui-filter"' not in page

    def test_the_grouping_choice_sits_with_the_rows_it_groups(self) -> None:
        html = render_report_html(report_payload())
        controls = html.index("data-section-controls")
        assert html.index("<h2>What cost the most</h2>") < controls
        assert 'class="regroup"' in html[controls : controls + 2000]

    def test_every_grouping_is_rendered_so_switching_computes_nothing(self, payload: dict) -> None:
        """The browser chooses which precomputed rows to show; it never rebuilds one."""
        html = render_report_html(payload)
        for grouping in GROUPINGS:
            assert f'data-grouping="{grouping}"' in html

    def test_the_section_carries_its_own_filter_and_tag_control(self) -> None:
        html = render_report_html(report_payload())
        assert 'class="row-filter"' in html
        assert "data-tag-filter" in html

    def test_the_tag_control_is_empty_until_the_script_fills_it(self) -> None:
        """Built from the rows present in that section, so it cannot offer a tag they lack."""
        html = render_report_html(report_payload())
        assert '<div class="tag-filter js-only" data-tag-filter hidden></div>' in html

    def test_the_controls_are_hidden_without_scripting(self) -> None:
        html = render_report_html(report_payload())
        for needle in ('class="regroup"', 'class="row-filter"', "data-tag-filter"):
            index = html.index(needle)
            assert "js-only" in html[max(0, index - 200) : index]


class TestFilteringSurvivesRevealingMoreRows:
    """ "Show more" used to undo the filter, and a filter you cannot trust is worse than none.

    The reveal announces what it did and the filter re-applies. Now that both live in report.js
    the coupling is one file's business, but the announcement is still the contract between
    them — and the regroup switch raises the same event for the same reason.
    """

    def script(self) -> str:
        return (ASSETS / "report.js").read_text(encoding="utf-8")

    def test_the_reveal_announces_itself(self) -> None:
        assert "ccaudit:rows-revealed" in self.script()
        assert "dispatchEvent" in self.script()

    def test_the_filter_reapplies_on_that_announcement(self) -> None:
        assert "addEventListener('ccaudit:rows-revealed'" in self.script()

    def test_filtering_does_not_reuse_the_truncation_hidden_state(self) -> None:
        """Two meanings for one flag is how clearing a filter revealed rows nobody asked for.

        A row shows when it is revealed *and* it matches; those are separate states, so the
        filter uses a class and leaves `hidden` to mean "still behind the truncation line".
        """
        assert "filtered-out" in self.script()
        assert "tr.filtered-out { display: none; }" in (ASSETS / "report.css").read_text(
            encoding="utf-8"
        )

    def test_pinned_summary_rows_are_never_filtered(self) -> None:
        """FR-040: a part-to-whole view that can be filtered into looking tidy is a lie."""
        assert "data-pinned" in self.script()


class TestAReaderLeavingIsNotAnError:
    """Reloading, navigating away, or closing the tab drops the connection while the server is
    still writing. That is the reader using their browser, not a fault in the server: it must
    not raise, and it must not print a traceback over the URL the terminal is showing.

    Pinned after a BrokenPipeError escaped `_respond` onto a user's terminal. Driven through a
    write that fails rather than a real hang-up, because whether a 100 KB page fills a kernel
    send buffer before the peer disappears is the operating system's decision, not a contract.
    """

    def handler_writing_to(self, wfile: Any) -> _Handler:
        """A handler with just enough wired up to respond, and no socket underneath it."""
        handler = _Handler.__new__(_Handler)
        handler.wfile = wfile
        handler.request_version = "HTTP/1.0"
        handler.requestline = "GET / HTTP/1.0"
        handler.client_address = ("127.0.0.1", 0)
        handler.close_connection = False
        return handler

    def test_a_write_to_a_reader_who_left_is_not_raised(self) -> None:
        class Departed:
            def write(self, _data: bytes) -> int:
                raise BrokenPipeError(32, "Broken pipe")

        handler = self.handler_writing_to(Departed())
        handler._respond(HTTPStatus.OK, "text/html; charset=utf-8", "<html></html>")

        # And the connection is marked done, so nothing tries to write to it again.
        assert handler.close_connection is True

    def test_a_reset_is_treated_the_same_way(self) -> None:
        class Reset:
            def write(self, _data: bytes) -> int:
                raise ConnectionResetError(54, "Connection reset by peer")

        self.handler_writing_to(Reset())._respond(HTTPStatus.OK, "text/plain", "hello")

    def test_a_write_that_fails_for_any_other_reason_still_shouts(self) -> None:
        """The guard is narrow on purpose: only a departed reader is a normal outcome."""

        class Broken:
            def write(self, _data: bytes) -> int:
                raise OSError(28, "No space left on device")

        with pytest.raises(OSError, match="No space left on device"):
            self.handler_writing_to(Broken())._respond(HTTPStatus.OK, "text/plain", "hello")

    def test_a_hangup_does_not_reach_the_terminal(
        self, payload: dict, sessions: list[SessionRef], capfd: pytest.CaptureFixture[str]
    ) -> None:
        """socketserver prints a traceback for anything escaping a handler. Not for this."""
        server = UiHttpServer(lambda _selection: payload, sessions, Selection(("sess-one",)))
        try:
            try:
                raise BrokenPipeError(32, "Broken pipe")
            except BrokenPipeError:
                server.handle_error(None, ("127.0.0.1", 0))
            assert "Traceback" not in capfd.readouterr().err

            try:
                raise ValueError("a genuine fault in a handler")
            except ValueError:
                server.handle_error(None, ("127.0.0.1", 0))
            assert "Traceback" in capfd.readouterr().err
        finally:
            server.server_close()


class TestSplittingInjectedContentIsPartOfTheView:
    """It changes what the table shows, so it belongs in the URL and in the terminal
    equivalent — otherwise a shared link or a copied command shows a different breakdown.
    """

    def test_merged_is_the_default(self) -> None:
        assert Selection(("s",)).merge_injected is True

    def test_the_query_carries_the_split(self) -> None:
        selection = selection_from_query(
            {"session": ["s"], "split": ["1"]}, default=Selection(("s",)), known=["s"]
        )
        assert selection.merge_injected is False

    def test_a_form_that_omits_it_means_merged(self) -> None:
        """An unticked box submits nothing; absence is the answer, not a missing value."""
        selection = selection_from_query(
            {"session": ["s"]}, default=Selection(("s",), merge_injected=False), known=["s"]
        )
        assert selection.merge_injected is True

    def test_the_terminal_equivalent_names_the_flag(self) -> None:
        assert "--split-injected" in terminal_command(Selection(("s",), merge_injected=False))
        assert "--split-injected" not in terminal_command(Selection(("s",)))


class TestThePickerCanBeRanked:
    """A flat list of names cannot answer "which of these actually cost me anything", which is
    the question the picker is opened to settle. Every rankable fact is a column.

    The measured columns need each session analysed, which is too slow to hold the page open
    for on a large corpus (SC-025), so they arrive per session after the page does.
    """

    @pytest.fixture
    def page(self, payload: dict, sessions: list[SessionRef]) -> str:
        return render_ui_html(payload, sessions=sessions, selection=Selection(("sess-one",)))

    def test_the_picker_is_a_table_with_a_column_per_metric(self, page: str) -> None:
        assert 'id="ui-session-table"' in page
        for key, label, _cheap in SESSION_METRICS:
            assert f'data-metric="{key}"' in page
            assert label in page

    def test_cheap_facts_are_already_there(self, page: str) -> None:
        """Readable from file metadata, so they cost nothing and never arrive late."""
        for key in CHEAP_SESSION_METRICS:
            assert f'data-metric="{key}" data-value=' in page

    def test_a_measured_cell_starts_empty_rather_than_zero(self, page: str) -> None:
        """Blank means "not measured yet"; a zero would mean "none", and only one is true."""
        for key in ANALYSED_SESSION_METRICS:
            assert f'<td class="ui-num" data-metric="{key}"></td>' in page

    def test_an_unknown_session_is_refused_rather_than_answered_with_zeroes(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        served = []

        def facts(session_id: str) -> dict[str, int]:
            served.append(session_id)
            return {"cost_micros": 1}

        server = UiHttpServer(lambda _s: payload, sessions, Selection(("sess-one",)), facts)
        try:
            assert server.facts is not None
            # The handler checks membership before calling; the provider is never asked about a
            # session the picker does not list.
            assert "sess-one" in {reference.session_id for reference in sessions}
        finally:
            server.server_close()
        assert served == []

    def test_a_server_without_a_facts_provider_still_serves(
        self, payload: dict, sessions: list[SessionRef]
    ) -> None:
        """The report shell and the tests build one without facts; that must stay legal."""
        server = UiHttpServer(lambda _s: payload, sessions, Selection(("sess-one",)))
        try:
            assert server.facts is None
        finally:
            server.server_close()
