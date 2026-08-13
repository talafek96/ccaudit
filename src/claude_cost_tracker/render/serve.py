"""The interactive surface — a command that happens to render in a browser, not a service.

This is the exploration shell: pick sessions, regroup, filter, sort, drill in. It is deliberately
the *thinnest possible* thing that can do that, because the hard constraint here is not features,
it is that nothing is left running (Principle II, FR-073, SC-025):

**Stdlib only.** ``http.server`` on ``127.0.0.1`` with an OS-assigned port. No framework, no ASGI
server, no dependency that would turn a read-only JSON endpoint into a service someone forgets to
stop.

**One renderer, two shells** (research.md §2). The page served here is
``render_report_html`` — the same renderer as the shareable report, over the same payload. The UI
adds controls around it and swaps the whole page on a new selection. JavaScript sorts, filters,
and folds; it never computes a figure. That is what makes FR-074 true by construction rather than
by discipline: nothing can be exclusive to the browser if the browser computes nothing.

**Nothing is fetched from outside.** Every asset is inlined and every link is relative. The only
network this process speaks is the loopback socket it opened (FR-073).

**It refuses to serve a breakdown that does not add up.** Checked here, at the edge, as it is in
``render/data.py`` and ``render/report.py`` — an exploration surface that quietly served a
non-reconciling payload would be the worst place for it, because it is the one a reader clicks
around in and trusts.
"""

import json
import logging
import socketserver
import sys
import threading
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

from claude_cost_tracker.ingest.discover import SessionRef
from claude_cost_tracker.model.reconcile import ReconciliationError
from claude_cost_tracker.render.data import DEFAULT_GROUPING, GROUPINGS, SESSION_METRICS
from claude_cost_tracker.render.report import ASSETS, render_report_html

LOOPBACK = "127.0.0.1"
UI_STYLESHEET = ASSETS / "ui.css"
UI_SCRIPT = ASSETS / "ui.js"

_LOGGER = logging.getLogger("claude_cost_tracker.ui")

_EXPLORE_NOTE = (
    "This view is for exploring: it changes as you click, and it disappears when you stop the "
    "server. It is not the shareable artifact — `ccost report` writes that as one frozen file."
)
_TERMINAL_NOTE = (
    "Nothing here is browser-only. The same selection from the terminal, with the same figures:"
)
_SESSION_COLUMN = (
    "What the session was about, the first block of its id, the directory Claude Code was "
    "started from, and when it was last written."
)
_FILTER_NOTE = (
    "Filtering hides rows, never cost: the totals, the summary rows, and the unattributed "
    "remainder always describe the whole selection."
)
_STOPPED_PAGE = (
    "<!doctype html>\n"
    '<html lang="en"><head><meta charset="utf-8"><title>claude-cost-tracker — stopped</title></head>\n'
    "<body><h1>Stopped</h1><p>The ccost interactive server has shut down and is no longer "
    "listening. You can close this tab.</p></body></html>\n"
)


@dataclass(frozen=True)
class Selection:
    """What the user is currently looking at — the whole state of the interactive view.

    It round-trips through the query string, so every view has a URL, and that URL maps one-to-one
    onto a terminal invocation (FR-074).
    """

    session_ids: tuple[str, ...]
    group_by: str = DEFAULT_GROUPING
    redact: bool = False
    merge_injected: bool = True


# Given a selection, produce a report-data payload. The server holds no analysis logic of its
# own: it renders whatever this returns, or refuses to.
PayloadProvider = Callable[[Selection], Mapping[str, Any]]

# Given one session id, the rankable facts about it (`render.data.session_facts`), or ``None``
# when it has none to give — a session this rate table cannot price at all. Separate from the
# payload provider because it is answered one session at a time: the picker has to be usable
# before the corpus has been analysed, so these arrive after the page does.
FactsProvider = Callable[[str], Mapping[str, int] | None]


def terminal_command(selection: Selection) -> str:
    """The terminal invocation that produces the very figures currently on screen (FR-074)."""
    parts = ["ccost"]
    for session_id in selection.session_ids:
        parts += ["--session", session_id]
    parts += ["--by", selection.group_by]
    if selection.redact:
        parts.append("--redact")
    if not selection.merge_injected:
        parts.append("--split-injected")
    return " ".join(parts)


def payload_json(payload: Mapping[str, Any]) -> str:
    """Serialise exactly as ``ccost --json`` does, trailing newline included.

    Byte-identical output is the checkable form of FR-074: if the browser and the terminal hand
    back the same bytes for the same selection, neither can be showing the other something extra.
    """
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def render_ui_html(
    payload: Mapping[str, Any], *, sessions: Sequence[SessionRef], selection: Selection
) -> str:
    """Wrap the report document in the interactive controls.

    Raises ``ValueError`` if the report shell no longer has the shape this wraps — better to stop
    than to serve a page with the controls silently missing or in the wrong place.
    """
    document = render_report_html(payload)
    head_close = "</head>\n<body>\n"
    body_close = "</body>\n</html>\n"
    if head_close not in document or body_close not in document:
        raise ValueError(
            "the report shell no longer has the head/body markers the interactive UI wraps; "
            "render/serve.py and render/report.py have drifted apart"
        )
    style = f"<style>\n{UI_STYLESHEET.read_text(encoding='utf-8')}</style>\n"
    script = f"<script>\n{UI_SCRIPT.read_text(encoding='utf-8')}</script>\n"
    document = document.replace(
        head_close, f"{style}{head_close}{_controls(sessions, selection)}", 1
    )
    return document.replace(body_close, f"{script}{body_close}", 1)


def _controls(sessions: Sequence[SessionRef], selection: Selection) -> str:
    """The controls, server-rendered so the page is usable with scripting disabled.

    The session picker and the grouping switch are a plain GET form: they work as navigation.
    Everything the script adds on top (filtering, folding, hiding sections) is presentation, and
    is hidden until the script runs so the page never shows a dead control.
    """
    chosen = set(selection.session_ids)
    if sessions:
        options = _session_table(sessions, chosen)
    else:
        options = '<p class="ui-empty">No other local sessions were found.</p>'

    return "".join(
        [
            '<nav class="ui" aria-label="Explore this session">',
            '<div class="ui-bar">',
            '<span class="ui-brand">claude-cost-tracker</span>',
            '<span class="ui-mode">exploring</span>',
            '<span class="ui-spacer"></span>',
            '<form method="post" action="/shutdown" class="ui-inline">',
            '<button type="submit" class="ui-btn ui-btn--quiet">Stop the server</button>',
            "</form>",
            "</div>",
            '<form method="get" action="/" class="ui-form">',
            '<section class="ui-panel ui-panel--sessions">',
            '<header class="ui-panel-head">',
            "<h2>Sessions</h2>",
            '<span id="ui-selected" class="ui-count"></span>',
            '<span id="ui-facts-status" class="ui-count"></span>',
            '<span class="ui-spacer"></span>',
            '<span class="ui-actions js-only">',
            '<button type="button" id="ui-all" class="ui-btn ui-btn--quiet">All</button>',
            '<button type="button" id="ui-none" class="ui-btn ui-btn--quiet">None</button>',
            "</span>",
            "</header>",
            options,
            (
                '<p class="ui-hint">Every figure on the page is recomputed for the sessions '
                "ticked here. Click a column to rank by it — the measured columns fill in as "
                "each session is analysed, and a blank cell means not measured yet, never "
                "zero.</p>"
            ),
            "</section>",
            '<section class="ui-panel">',
            '<header class="ui-panel-head"><h2>View</h2></header>',
            '<div class="ui-fields">',
            f'<input type="hidden" name="by" value="{escape(selection.group_by)}">',
            (
                '<label class="ui-check"><input type="checkbox" name="redact" value="1"'
                f"{' checked' if selection.redact else ''}>"
                "<span>Redact paths</span></label>"
            ),
            (
                '<label class="ui-check" title="Claude Code injects the same skill listing and '
                "tool schemas into every project. Merged, they are one row; split, there is one "
                'per project, named after it.">'
                '<input type="checkbox" name="split" value="1"'
                f"{'' if selection.merge_injected else ' checked'}>"
                "<span>Split injected content by project</span></label>"
            ),
            '<button type="submit" id="ui-apply" class="ui-btn ui-btn--primary">Apply</button>',
            "</div>",
            f'<p class="ui-hint js-only">{escape(_FILTER_NOTE)}</p>',
            "</section>",
            "</form>",
            '<section class="ui-panel js-only">',
            '<header class="ui-panel-head"><h2>Sections</h2>',
            '<span class="ui-spacer"></span>',
            '<span class="ui-actions">',
            (
                '<button type="button" id="ui-expand" class="ui-btn ui-btn--quiet">'
                "Expand drill-downs</button>"
            ),
            '<button type="button" id="ui-collapse" class="ui-btn ui-btn--quiet">Collapse</button>',
            "</span></header>",
            '<div class="ui-views" id="ui-views"></div>',
            "</section>",
            '<details class="ui-details">',
            "<summary>The same selection from the terminal</summary>",
            f'<p class="ui-hint">{escape(_TERMINAL_NOTE)}</p>',
            f"<code>{escape(terminal_command(selection))}</code>",
            f'<p class="ui-hint">{escape(_EXPLORE_NOTE)}</p>',
            "</details>",
            "</nav>",
        ]
    )


def _session_table(sessions: Sequence[SessionRef], chosen: set[str]) -> str:
    """The picker as a ranked table: one toggleable row per session, one column per fact.

    A flat list of twenty-six names cannot answer "which of these actually cost me anything",
    which is the question someone opens this to ask. So every fact the picker can rank by is a
    column, and the header sorts by it.

    Only the facts readable from the transcript's metadata are filled in here. The rest need the
    session analysed, which is too slow to hold the page open for on a large corpus (SC-025), so
    those cells start empty and the script fills them as answers arrive.
    """
    headers = "".join(
        # The description is the column's contract with the reader: "Reads" and ".md reads"
        # answer different questions, and a header that has to be guessed is one people rank
        # by and then misread.
        f'<th class="ui-num ui-sortable-head" data-metric="{escape(metric.key)}" '
        f'data-tip="{escape(metric.description)}" title="{escape(metric.description)}">'
        f"{escape(metric.label)}</th>"
        for metric in SESSION_METRICS
    )
    return "".join(
        [
            '<div class="ui-table-scroll"><table class="ui-sessions-table" id="ui-session-table">',
            "<thead><tr>",
            '<th class="ui-pick"><span class="ui-sr">Include</span></th>',
            (
                f'<th class="ui-sortable-head" data-metric="name" '
                f'data-tip="{escape(_SESSION_COLUMN)}" title="{escape(_SESSION_COLUMN)}">'
                f"Session</th>"
            ),
            headers,
            "</tr></thead><tbody>",
            "".join(
                _session_row(reference, reference.session_id in chosen) for reference in sessions
            ),
            "</tbody></table></div>",
        ]
    )


def _human_bytes(size: int) -> str:
    """A transcript size a reader can compare at a glance. 52,085,619 is not one."""
    if size >= 1_000_000:
        return f"{size / 1_000_000:.0f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} kB"
    return f"{size} B"


def _session_row(reference: SessionRef, checked: bool) -> str:
    """One session: what it was about, what identifies it, and what it is worth ranking by."""
    project = str(reference.project_path) if reference.project_path else reference.project_dir
    running = '<span class="ui-tag">running</span>' if reference.in_progress else ""
    name = reference.title or "(unnamed session)"
    known = {"records": reference.record_count, "bytes": reference.byte_size}
    shown = {"records": f"{reference.record_count:,}", "bytes": _human_bytes(reference.byte_size)}
    cells = "".join(
        # A cheap fact is written as text *and* as a sort key; an analysed one carries neither
        # until it is known, so an empty cell is never mistaken for a zero. The sort key stays
        # the raw number, so ranking by size is not ranking by the rounded label.
        f'<td class="ui-num" data-metric="{escape(metric.key)}"'
        + (f' data-value="{known[metric.key]}">{shown[metric.key]}' if metric.key in known else ">")
        + "</td>"
        for metric in SESSION_METRICS
    )
    return (
        f'<tr class="ui-session-row" data-session="{escape(reference.session_id)}" '
        f'data-name="{escape(name.lower())}">'
        f'<td class="ui-pick"><input type="checkbox" name="session" '
        f'value="{escape(reference.session_id)}"{" checked" if checked else ""} '
        f'aria-label="{escape(name)}"></td>'
        f'<td class="ui-session-cell" title="{escape(reference.session_id)}">'
        f'<span class="ui-session-title">'
        f'<span class="ui-session-name">{escape(name)}</span>{running}</span>'
        f'<span class="ui-session-meta">{escape(reference.short_id)} · {escape(project)}'
        f" · {reference.modified_at:%Y-%m-%d %H:%M}</span>"
        f"</td>{cells}</tr>"
    )


def _session_label(reference: SessionRef) -> str:
    project = str(reference.project_path) if reference.project_path else reference.project_dir
    running = " · still running" if reference.in_progress else ""
    # Name first, then the id fragment that selects it. A picker listing 900 UUIDs gives a
    # reader no way to find the session they mean.
    return (
        f"{reference.display_name} — {project} · {reference.record_count:,} records · "
        f"{reference.modified_at:%Y-%m-%d %H:%M}{running}"
    )


def selection_from_query(
    query: Mapping[str, Sequence[str]], *, default: Selection, known: Sequence[str]
) -> Selection:
    """Read a selection out of a query string, refusing anything it cannot honour.

    Raises ``ValueError`` on an unknown grouping or an unknown session id. A view that quietly
    fell back to the default would show one selection while claiming another.
    """
    # An empty query is the bare URL — the view the command was started on. Anything else came
    # from the form, where an unchecked box submits nothing at all: absence there is a choice,
    # not a gap, and must not be filled in from the default.
    if not query:
        return default

    session_ids = tuple(query.get("session", ()))
    if session_ids:
        unknown = [value for value in session_ids if value not in known]
        if unknown:
            raise ValueError(f"unknown session(s): {', '.join(unknown)}")
    else:
        session_ids = default.session_ids

    group_by = (query.get("by") or [default.group_by])[0]
    if group_by not in GROUPINGS:
        raise ValueError(f"unknown grouping {group_by!r}; known: {', '.join(GROUPINGS)}")

    # Absence is a choice here, like `redact`: the form submits `split` only when it is ticked,
    # so a query that came from the form and omits it means "merged", not "unspecified".
    return Selection(
        session_ids=session_ids,
        group_by=group_by,
        redact=bool(query.get("redact")),
        merge_injected=not query.get("split"),
    )


class _Handler(BaseHTTPRequestHandler):
    """The whole HTTP surface: two GETs for the views, one for the sessions, one POST to stop."""

    # HTTP/1.0 so every response closes its connection: no keep-alive socket can outlive the
    # shutdown and keep the port occupied, which is the property SC-025 is really about.
    protocol_version = "HTTP/1.0"
    server_version = "ccost"
    sys_version = ""

    @property
    def ui(self) -> "UiHttpServer":
        server = self.server
        if not isinstance(server, UiHttpServer):
            raise TypeError(f"handler attached to an unexpected server: {type(server).__name__}")
        return server

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        try:
            if route.path == "/":
                selection = self._selection(route.query)
                body = render_ui_html(
                    self.ui.payload_for(selection),
                    sessions=self.ui.sessions,
                    selection=selection,
                )
                self._respond(HTTPStatus.OK, "text/html; charset=utf-8", body)
            elif route.path == "/data":
                selection = self._selection(route.query)
                self._respond(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    payload_json(self.ui.payload_for(selection)),
                )
            elif route.path == "/favicon.ico":
                # Browsers ask for this unprompted. Answering "no content" is the honest reply
                # and keeps the console clean; a 404 there reads as something being broken, on
                # a page whose whole claim is that nothing is fetched from off the machine.
                self._respond(HTTPStatus.NO_CONTENT, "image/x-icon", "")
            elif route.path == "/facts":
                # One session per request, so the picker is usable immediately and fills in as
                # answers arrive (SC-025). An unanalysable session answers 404 rather than a
                # zero: a blank cell says "not known", a zero would say "none", and only one of
                # those is true (Principle X).
                wanted = (parse_qs(route.query).get("session") or [""])[0]
                facts = self.ui.facts
                known = {reference.session_id for reference in self.ui.sessions}
                if facts is None or wanted not in known:
                    self._respond(
                        HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", "no such session\n"
                    )
                    return
                measured = facts(wanted)
                if measured is None:
                    self._respond(
                        HTTPStatus.NOT_FOUND,
                        "text/plain; charset=utf-8",
                        "session cannot be analysed\n",
                    )
                    return
                self._respond(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    json.dumps(dict(measured)) + "\n",
                )
            elif route.path == "/sessions":
                self._respond(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    json.dumps([_session_label(ref) for ref in self.ui.sessions], indent=2) + "\n",
                )
            else:
                self._respond(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", "no such view\n")
        except ReconciliationError as error:
            # Recovery here means telling the reader instead of drawing it. The one thing that
            # must not happen is a page of figures that do not add up (Principle X).
            _LOGGER.error("refusing to serve a breakdown that does not add up: %s", error)
            self._respond(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "text/plain; charset=utf-8",
                f"refusing to serve a breakdown that does not add up: {error}\n",
            )
        except ValueError as error:
            # A request that names something we do not have is a normal outcome to report back,
            # not a failure of the server.
            self._respond(HTTPStatus.BAD_REQUEST, "text/plain; charset=utf-8", f"{error}\n")

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/shutdown":
            self._respond(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", "no such action\n")
            return
        self._respond(HTTPStatus.OK, "text/html; charset=utf-8", _STOPPED_PAGE)
        self.ui.request_shutdown()

    def _selection(self, query: str) -> Selection:
        return selection_from_query(
            parse_qs(query),
            default=self.ui.initial,
            known=[reference.session_id for reference in self.ui.sessions],
        )

    def _respond(self, status: HTTPStatus, content_type: str, body: str) -> None:
        encoded = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            # The page is built from local records for one reader; a cached copy of a stale
            # selection is only ever confusing.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError) as error:
            # The reader reloaded, navigated away, or closed the tab before the response
            # finished. Nothing is broken and there is nobody left to tell, so the only honest
            # move is to stop writing to a socket no one holds — not to raise, which would
            # spray a traceback across a terminal whose whole job is to be showing a URL.
            _LOGGER.debug("client left before the response finished: %s", error)
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        """Requests go to the log, not to the user's terminal, which is showing a URL."""
        _LOGGER.debug("ui %s", format % args)


class UiHttpServer(ThreadingHTTPServer):
    """Loopback-only, OS-assigned port, and it knows how to stop itself."""

    daemon_threads = True

    def __init__(
        self,
        provider: PayloadProvider,
        sessions: Sequence[SessionRef],
        initial: Selection,
        facts: FactsProvider | None = None,
    ) -> None:
        super().__init__((LOOPBACK, 0), _Handler)
        self.provider = provider
        self.sessions = tuple(sessions)
        self.initial = initial
        self.facts = facts

    def server_bind(self) -> None:
        """Bind without resolving this machine's name.

        ``HTTPServer.server_bind`` calls ``socket.getfqdn`` purely to fill in ``server_name``,
        which is used only by CGI headers this server never sends. Measured on a machine whose
        hostname does not resolve, that lookup blocked for **35 seconds** — seven times the whole
        budget in SC-025, before a single figure had been computed. The address is known: it is
        loopback, because that is the only thing this binds to.
        """
        socketserver.TCPServer.server_bind(self)
        self.server_name = LOOPBACK
        self.server_port = int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Keep a client hanging up out of the terminal, without hiding a real fault.

        ``socketserver`` prints a traceback for anything that escapes a handler. A reader
        reloading the page can drop the connection mid-read as well as mid-write, and that is
        not a fault of this server — it is the reader exercising their browser. Everything
        else still surfaces the way it always did.
        """
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            _LOGGER.debug("client %s hung up mid-request", client_address)
            return
        super().handle_error(request, client_address)

    def payload_for(self, selection: Selection) -> Mapping[str, Any]:
        """Build the payload and refuse it if its parts contradict its total."""
        payload = self.provider(selection)
        totals = payload["totals"]
        if totals["attributed_micros"] + totals["unattributed_micros"] != totals["cost_micros"]:
            raise ReconciliationError(
                f"{totals['attributed_micros']} + {totals['unattributed_micros']} != "
                f"{totals['cost_micros']}"
            )
        return payload

    def request_shutdown(self) -> None:
        """Stop the serving loop from inside a request, without deadlocking on it."""
        threading.Thread(target=self.shutdown, daemon=True).start()


class UiServer:
    """The server's lifetime, made explicit so it cannot outlive the command that started it.

    Used as a context manager it serves in a background thread and is guaranteed stopped and its
    socket closed on exit, including on an exception. ``run`` is the foreground form.
    """

    def __init__(
        self,
        provider: PayloadProvider,
        sessions: Sequence[SessionRef],
        initial: Selection,
        facts: FactsProvider | None = None,
    ) -> None:
        self._http = UiHttpServer(provider, sessions, initial, facts)
        self._serving = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def port(self) -> int:
        return int(self._http.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}/"

    def run(self) -> None:
        """Serve in this thread until stopped. Always leaves the socket closed."""
        self._serving.set()
        try:
            self._http.serve_forever(poll_interval=0.1)
        finally:
            self._serving.clear()
            self.close()

    def shutdown(self) -> None:
        """Ask the serving loop to stop. Safe before it starts and after it has finished."""
        if self._serving.is_set():
            self._http.shutdown()

    def close(self) -> None:
        """Stop serving and release the port. Idempotent, so every exit path can call it."""
        if self._closed:
            return
        self._closed = True
        self.shutdown()
        self._http.server_close()

    def __enter__(self) -> Self:
        thread = threading.Thread(target=self.run, name="ccost-ui", daemon=True)
        self._thread = thread
        thread.start()
        # Serving has begun before the caller gets the URL, so a request cannot race the loop.
        self._serving.wait(timeout=5)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def serve_ui(
    provider: PayloadProvider,
    sessions: Sequence[SessionRef],
    initial: Selection,
    *,
    facts: FactsProvider | None = None,
    open_browser: bool = True,
    announce: Callable[[str], None] = print,
) -> None:
    """Start the interactive interface and block until it is stopped (FR-072, FR-073).

    Returns once the server has stopped and its port is free — by the Stop button, by Ctrl-C, or
    by an error. Nothing survives this call: no thread, no socket, no background work.

    ``open_browser`` opens the local URL in the user's browser; it is a loopback URL, and no
    request leaves the machine either way.
    """
    server = UiServer(provider, sessions, initial, facts)
    announce(f"ccost is serving on {server.url} — press Ctrl-C to stop it.")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl-C is how this is normally ended; `run` has already closed the socket in its
        # `finally`, so there is nothing left to clean up and nothing to report as a failure.
        announce("")
    finally:
        server.close()
    announce("ccost has stopped. Nothing is left running.")
