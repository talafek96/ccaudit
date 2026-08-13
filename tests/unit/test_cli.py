"""Contract on the command-line surface.

The exit codes are part of the interface — a script branching on them must keep working — and
code 3 in particular must stay reachable only from a reconciliation failure, never from an
ordinary warning path.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from ccaudit.cli import (
    EXIT_DATA_ERROR,
    EXIT_DOES_NOT_ADD_UP,
    EXIT_INTERRUPTED,
    EXIT_NO_SESSIONS,
    EXIT_OK,
    EXIT_USAGE,
    DataError,
    NoSessionsFound,
    ReconciliationError,
    UsageError,
    build_parser,
    configure_logging,
    main,
    select_sessions,
)
from ccaudit.ingest.discover import encode_project_dir
from tests.fixtures.builder import TranscriptBuilder, simple_session


class TestExitCodes:
    def test_the_codes_are_distinct(self) -> None:
        codes = [
            EXIT_OK,
            EXIT_USAGE,
            EXIT_NO_SESSIONS,
            EXIT_DOES_NOT_ADD_UP,
            EXIT_DATA_ERROR,
            EXIT_INTERRUPTED,
        ]
        assert len(set(codes)) == len(codes)

    def test_does_not_add_up_has_its_own_code(self) -> None:
        """It must never be mistaken for a warning or for an ordinary data error."""
        assert EXIT_DOES_NOT_ADD_UP == 3
        assert EXIT_DOES_NOT_ADD_UP not in (EXIT_OK, EXIT_DATA_ERROR, EXIT_USAGE)

    def test_reconciliation_failure_is_an_assertion_not_a_value(self) -> None:
        """A breakdown that does not add up is a broken invariant, not a returnable outcome."""
        assert issubclass(ReconciliationError, AssertionError)

    def test_no_sessions_is_not_an_error_type(self) -> None:
        """An empty selection is a normal outcome the caller branches on, not a failure."""
        assert issubclass(NoSessionsFound, LookupError)


class TestParser:
    def test_zero_arguments_parses(self) -> None:
        """Zero arguments is a complete invocation (FR-048), not a usage error."""
        args = build_parser().parse_args([])
        assert args.command is None

    def test_version_flag_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert "ccaudit" in capsys.readouterr().out

    def test_an_unknown_command_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["nonsense"])
        assert exc.value.code == EXIT_USAGE


class TestErrorTranslation:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (UsageError("bad selection"), EXIT_USAGE),
            (NoSessionsFound("nothing matched"), EXIT_NO_SESSIONS),
            (ReconciliationError("off by 3 micros"), EXIT_DOES_NOT_ADD_UP),
            (DataError("record 4 unreadable"), EXIT_DATA_ERROR),
            (KeyboardInterrupt(), EXIT_INTERRUPTED),
        ],
    )
    def test_each_failure_maps_to_its_documented_code(
        self,
        error: BaseException,
        expected: int,
        monkeypatch: pytest.MonkeyPatch,
        ccaudit_home: Path,
    ) -> None:
        def explode(*_: object, **__: object) -> int:
            raise error

        monkeypatch.setattr("ccaudit.cli._run_pricing", explode)
        assert main(["pricing", "show"]) == expected

    def test_a_reconciliation_failure_says_it_is_our_defect(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        ccaudit_home: Path,
    ) -> None:
        def explode(*_: object, **__: object) -> int:
            raise ReconciliationError("attributed 5 but total is 7")

        monkeypatch.setattr("ccaudit.cli._run_pricing", explode)
        main(["pricing", "show"])
        stderr = capsys.readouterr().err
        assert "does not add up" in stderr
        assert "defect in ccaudit" in stderr


class TestPricingCommand:
    def test_show_names_the_table_and_its_date(
        self, capsys: pytest.CaptureFixture[str], ccaudit_home: Path
    ) -> None:
        assert main(["pricing", "show"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "rates as published" in out
        assert "API-equivalent cost estimates" in out
        assert "not billed amounts" in out

    def test_show_tells_you_how_to_update_bundled_rates(
        self,
        capsys: pytest.CaptureFixture[str],
        ccaudit_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rates are not pinned to the release, and the surface has to say so."""
        monkeypatch.delenv("CCAUDIT_PRICING", raising=False)
        main(["pricing", "show"])
        assert "ccaudit pricing refresh" in capsys.readouterr().out

    def test_refresh_from_a_local_file_needs_no_network(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], ccaudit_home: Path
    ) -> None:
        source = tmp_path / "rates.json"
        source.write_text(
            json.dumps(
                {
                    "claude-opus-5": {
                        "litellm_provider": "anthropic",
                        "input_cost_per_token": 5e-06,
                        "output_cost_per_token": 2.5e-05,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert main(["pricing", "refresh", "--from", str(source), "--dry-run"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "dry run" in out
        assert not (ccaudit_home / "pricing.toml").exists()

    def test_both_source_flags_at_once_is_a_usage_error(
        self, tmp_path: Path, ccaudit_home: Path
    ) -> None:
        code = main(
            [
                "pricing",
                "refresh",
                "--source-url",
                "https://example/x.json",
                "--from",
                str(tmp_path),
            ]
        )
        assert code == EXIT_USAGE

    def test_a_bad_source_leaves_rates_unchanged_and_exits_data_error(
        self, tmp_path: Path, ccaudit_home: Path
    ) -> None:
        assert main(["pricing", "refresh", "--from", str(tmp_path / "absent.json")]) == (
            EXIT_DATA_ERROR
        )
        assert not (ccaudit_home / "pricing.toml").exists()


class TestLogging:
    def test_an_unwritable_state_directory_does_not_stop_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Analysis needs nothing from the log file; failing to open it must not be fatal."""
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("CCAUDIT_HOME", str(blocked / "state"))
        configure_logging(0)  # must not raise

    def test_logging_writes_to_the_state_directory(
        self, ccaudit_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hook path logs failures rather than surfacing them into a session (FR-054)."""
        configure_logging(0)
        assert (ccaudit_home / "ccaudit.log").exists()


class TestTheZeroArgumentDefault:
    """FR-048, amended 2026-08-12: the default scope is the project, not its newest session.

    Fenced here rather than only in discovery, because the flag that restores the old behaviour
    (`--latest`) and the order it composes with `--exclude` are both CLI-level decisions.
    """

    def corpus(self, claude_home: Path, project: Path, session_ids: list[str]) -> None:
        """One transcript per id, each newer than the last, in the project's encoded directory."""
        directory = claude_home / "projects" / encode_project_dir(project)
        directory.mkdir(parents=True, exist_ok=True)
        record = json.dumps(
            {
                "type": "assistant",
                "uuid": "u1",
                "requestId": "req_1",
                "message": {"id": "msg_1", "model": "claude-opus-5", "usage": {}},
            }
        )
        for age, session_id in enumerate(reversed(session_ids)):
            path = directory / f"{session_id}.jsonl"
            path.write_text(f"{record}\n", encoding="utf-8")
            os.utime(path, (1_000_000 + age, 1_000_000 + age))

    @pytest.fixture
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A project directory with three recorded sessions, and the cwd inside it."""
        home = tmp_path / "claude"
        work = tmp_path / "repo"
        work.mkdir()
        self.corpus(home, work, ["newest", "middle", "oldest"])
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
        monkeypatch.chdir(work)
        return work

    def selected(self, argv: list[str]) -> list[str]:
        return [ref.session_id for ref in select_sessions(build_parser().parse_args(argv))]

    def test_no_arguments_takes_the_whole_project(self, project: Path) -> None:
        assert self.selected([]) == ["newest", "middle", "oldest"]

    def test_latest_narrows_to_the_most_recent(self, project: Path) -> None:
        assert self.selected(["--latest"]) == ["newest"]

    def test_latest_means_the_newest_that_survived_exclusion(self, project: Path) -> None:
        """Narrowing after exclusion, so excluding the newest leaves the next one — not nothing."""
        assert self.selected(["--latest", "--exclude", "newest"]) == ["middle"]

    def test_a_subdirectory_still_resolves_to_the_project(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = project / "src" / "pkg"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        assert self.selected([]) == ["newest", "middle", "oldest"]


class TestTheInteractiveViewSurvivesAForeignSession:
    """`~/.claude` is shared, and `--all` selects every session in it — including other tools'.

    Reported on Windows as "`--all` fails". It did: the browser view raised
    `UnknownModelError` out of the request handler on the first page load and the connection
    was reset, because the UI's providers analysed each session directly instead of applying
    the skip-and-name policy `_analyse_selection` already documents for a sweep.

    Nothing here is Windows-specific — the trigger is one session priced by a model the rate
    table does not carry, which is a shared-directory problem on every platform.
    """

    FOREIGN_MODEL = "some-other-tool-model-1"

    @pytest.fixture
    def corpus(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A Claude home holding one priceable session and one written by another tool."""
        home = tmp_path / "claude"
        simple_session(session_id="ours", project_path="/repo/mine").write_to_project_tree(home)
        foreign = TranscriptBuilder(session_id="theirs", project_path="/repo/theirs")
        foreign.add_user_text("do the thing")
        foreign.add_turn(model=self.FOREIGN_MODEL, input_tokens=10, output_tokens=5)
        foreign.write_to_project_tree(home)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
        return home

    def providers(self, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict[str, Any]:
        """Run `ccaudit ui`, capturing what it hands the server instead of serving."""
        captured: dict[str, Any] = {}

        def fake_serve_ui(provider: Any, sessions: Any, **kwargs: Any) -> None:
            captured["provider"] = provider
            captured["facts"] = kwargs["facts"]
            captured["selection"] = kwargs["initial"]

        monkeypatch.setattr("ccaudit.cli.serve_ui", fake_serve_ui)
        assert main(argv) == EXIT_OK
        return captured

    def test_the_page_still_renders_and_names_what_it_could_not_price(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole bug: one unpriceable session used to take the entire view down."""
        captured = self.providers(monkeypatch, ["ui", "--all", "--no-open"])

        payload = captured["provider"](captured["selection"])

        assert payload["scope"]["sessions_included"] == ["ours"]
        # Named, not counted: "1 skipped" tells a reader nothing they can act on (Principle X).
        assert payload["scope"]["sessions_skipped"] == [f"theirs ({self.FOREIGN_MODEL})"]

    def test_facts_for_an_unpriceable_session_are_absent_rather_than_zero(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`None` becomes a 404 and blank cells; a zero would claim the session cost nothing."""
        captured = self.providers(monkeypatch, ["ui", "--all", "--no-open"])

        assert captured["facts"]("theirs") is None
        assert captured["facts"]("ours")["cost_micros"] > 0

    def test_a_selection_with_nothing_priceable_is_reported_not_raised(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `ValueError` is what the server answers as a 400; anything else kills the handler."""
        captured = self.providers(monkeypatch, ["ui", "--session", "theirs", "--no-open"])

        with pytest.raises(ValueError, match="cannot price"):
            captured["provider"](captured["selection"])
