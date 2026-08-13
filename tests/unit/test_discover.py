"""Contract on discovery and the coverage fingerprint.

Two invariants matter here. The transcript tree is **read-only** (FR-020) — discovery may
open and stat, never write. And a session that has advanced must be *detectable* without
re-reading its full records (FR-085), because that detection is what stops a stale figure from
being served as current (FR-084).
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_cost_tracker.ingest.discover import (
    IN_PROGRESS_WINDOW,
    MAX_TITLE_LENGTH,
    SHORT_ID_LENGTH,
    TranscriptTreeError,
    _readings,
    claude_home,
    decode_project_dir,
    discover_sessions,
    encode_project_dir,
    fingerprint_transcript,
    project_lookup_path,
    scan_transcript,
    session_ref,
    session_title,
    sessions_for_cwd,
    sessions_for_project,
)

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def _record(uuid: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "requestId": f"req_{uuid}",
            "message": {"id": f"msg_{uuid}", "model": "claude-opus-5", "usage": {}},
        }
    )


def _write_session(
    projects: Path, project_path: Path, session_id: str, record_uuids: list[str]
) -> Path:
    """Create one transcript inside the encoded project directory Claude Code would use."""
    directory = projects / encode_project_dir(project_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(f"{_record(uuid)}\n" for uuid in record_uuids), encoding="utf-8")
    return path


def _append_record(path: Path, uuid: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{_record(uuid)}\n")


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under ``root`` with its size and mtime — the read-only assertion's baseline."""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    }


@pytest.fixture
def claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated Claude home with ``CLAUDE_CONFIG_DIR`` pointed at it."""
    home = tmp_path / "claude"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


class TestClaudeHomeResolution:
    def test_claude_config_dir_overrides_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env var is honoured, which is also what fences tests off the developer's data."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))

        assert claude_home() == tmp_path / "elsewhere"

    def test_default_is_the_user_claude_directory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

        assert claude_home() == Path.home() / ".claude"


class TestProjectDirectoryEncoding:
    """The forward map onto Claude Code's directory names, on every platform.

    These assertions are written against *path strings*, not against the host's separator, so
    they mean the same thing on a POSIX CI runner as on the Windows machine that found the bug.
    That is deliberate: a Windows project directory is a name a Linux runner must still encode
    correctly, because ``~/.claude`` gets copied and synced between machines.

    The *rule* these pin was measured against a real corpus — 203 of 204 project directories
    matched it, checked against the ``cwd`` each transcript records. The *fixtures* are
    synthetic, and deliberately so: nothing here should depend on an account that exists on
    one machine, and only the shape of a path is under test, never whose path it is.
    """

    def test_encoding_a_path_matches_the_claude_code_form(self) -> None:
        assert (
            encode_project_dir(Path("/Users/x/projects/claude-cost-tracker"))
            == "-Users-x-projects-claude-cost-tracker"
        )

    def test_a_windows_drive_letter_keeps_its_colon_as_a_dash_of_its_own(self) -> None:
        """`C:\\Users\\alice\\source` → `C--Users-alice-source`: two dashes, not one.

        The colon is a separate character and becomes a separate `-`. An encoding that replaced
        only the separator produced `C:-Users-alice-source`, which is not a legal directory name
        on Windows and matched nothing — so every Windows project found zero of its sessions.
        """
        assert encode_project_dir(Path(r"C:\Users\alice\source")) == "C--Users-alice-source"
        assert encode_project_dir(Path("C:/Users/alice/source")) == "C--Users-alice-source"

    def test_encoding_does_not_depend_on_the_host_separator(self) -> None:
        """Both separator styles encode identically, so the answer is the same on any platform."""
        assert encode_project_dir(Path(r"\Users\x\p")) == encode_project_dir(Path("/Users/x/p"))

    def test_every_character_that_is_not_a_letter_or_digit_becomes_a_dash(self) -> None:
        """Not just separators. Dots, underscores and spaces go too — measured, not assumed.

        `C:\\Users\\alice\\.claude` is stored as `C--Users-alice--claude`, and an encoding that
        kept the dot missed it. This is why the bug was never Windows-only: `~/.claude` is a
        directory a POSIX user has too.
        """
        assert encode_project_dir(Path(r"C:\Users\alice\.claude")) == "C--Users-alice--claude"
        assert encode_project_dir(Path("/home/y/my_project v2")) == "-home-y-my-project-v2"

    def test_a_drive_root_does_not_encode_to_the_posix_root(self) -> None:
        """The ancestor walk ends at the drive root, so where it lands is not a detail.

        `C:\\` used to encode to `C:-`, and joining that onto the projects root left `pathlib`
        reading `C:` as a drive and dropping it — so the walk ended at the directory named `-`,
        which is where a POSIX machine records sessions run from `/`. A single foreign session
        in there was reported as this project's entire cost.
        """
        assert encode_project_dir(Path("/")) == "-"
        assert encode_project_dir(Path("C:/")) != "-"

    def test_a_windows_directory_name_decodes_back_to_its_drive(self) -> None:
        """Without this every Windows session reports its encoded name instead of its project."""
        assert decode_project_dir("C--Users-alice-source", must_exist=False) == Path(
            "C:/Users/alice/source"
        )

    def test_decoding_is_naive_and_therefore_lossy(self) -> None:
        """``-`` is legal in a directory name, so the encoding cannot be inverted reliably."""
        ambiguous = encode_project_dir(Path("/a/b/my-project"))

        assert ambiguous == encode_project_dir(Path("/a/b/my/project"))
        assert decode_project_dir(ambiguous, must_exist=False) == Path("/a/b/my/project")

    def test_a_path_that_does_not_exist_decodes_to_none_rather_than_a_guess(self) -> None:
        """Missing attribution beats wrong attribution (Principle X)."""
        assert decode_project_dir("-no-such-path-anywhere-12345") is None

    def test_a_name_that_is_not_the_encoded_form_decodes_to_none(self) -> None:
        assert decode_project_dir("some-other-directory") is None

    def test_lookup_encodes_rather_than_decodes_so_hyphens_are_safe(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """A project whose own name contains a hyphen is still found — the forward map is exact."""
        project = tmp_path / "my-project"
        project.mkdir()
        _write_session(claude_config / "projects", project, "s1", ["a"])

        assert [ref.session_id for ref in sessions_for_project(project)] == ["s1"]

    def test_a_windows_project_finds_the_directory_windows_wrote(self, claude_config: Path) -> None:
        """The bug, end to end: a Windows path must resolve to the name on disk.

        Runs on every platform, because the encoded name is an ordinary directory name
        everywhere and the encoding does not consult the host separator.
        """
        directory = claude_config / "projects" / "C--Users-alice-source"
        directory.mkdir(parents=True)
        (directory / "s1.jsonl").write_text(f"{_record('a')}\n", encoding="utf-8")

        found = sessions_for_project(Path(r"C:\Users\alice\source"))

        assert [ref.session_id for ref in found] == ["s1"]

    def test_a_windows_project_never_matches_another_machines_posix_directory(
        self, claude_config: Path
    ) -> None:
        """`C:\\Users\\alice\\source` is not `/Users/alice/source`, and must not resolve to it.

        `~/.claude` is shared and synced, so POSIX-encoded directories from other machines sit
        beside the local ones. The old encoding left a drive colon in the name, which `pathlib`
        stripped on the join — landing on exactly this directory and reporting a stranger's
        session as this project's.
        """
        projects = claude_config / "projects"
        foreign = projects / "-Users-alice-source"
        foreign.mkdir(parents=True)
        (foreign / "someone-elses.jsonl").write_text(f"{_record('a')}\n", encoding="utf-8")
        root = projects / "-"
        root.mkdir()
        (root / "also-not-ours.jsonl").write_text(f"{_record('b')}\n", encoding="utf-8")

        assert sessions_for_project(Path(r"C:\Users\alice\source")) == []
        assert sessions_for_project(Path("C:/")) == []


class TestDiscovery:
    def test_finds_every_session_in_the_tree(self, claude_config: Path, tmp_path: Path) -> None:
        projects = claude_config / "projects"
        _write_session(projects, tmp_path / "alpha", "s1", ["a", "b"])
        _write_session(projects, tmp_path / "alpha", "s2", ["c"])
        _write_session(projects, tmp_path / "beta", "s3", ["d"])

        assert {ref.session_id for ref in discover_sessions()} == {"s1", "s2", "s3"}

    def test_an_empty_tree_finds_nothing_rather_than_erroring(self, claude_config: Path) -> None:
        """No sessions is a normal outcome the caller reports, not a failure (Principle I)."""
        assert discover_sessions() == []

    def test_a_missing_projects_directory_finds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "never-created"))

        assert discover_sessions() == []

    def test_a_projects_path_that_is_a_file_raises_with_the_path(self, tmp_path: Path) -> None:
        """A broken tree shouts rather than silently reporting an empty corpus."""
        home = tmp_path / "claude"
        home.mkdir()
        (home / "projects").write_text("not a directory", encoding="utf-8")

        with pytest.raises(TranscriptTreeError, match="expected a directory of projects"):
            discover_sessions(home)

    def test_sessions_are_ordered_newest_first_and_deterministically(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        projects = claude_config / "projects"
        older = _write_session(projects, tmp_path / "alpha", "old", ["a"])
        newer = _write_session(projects, tmp_path / "alpha", "new", ["b"])
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

        runs = [[ref.session_id for ref in discover_sessions()] for _ in range(3)]

        assert runs == [["new", "old"]] * 3

    def test_a_session_carries_its_project_and_record_count(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        project = tmp_path / "alpha"
        project.mkdir()
        _write_session(claude_config / "projects", project, "s1", ["a", "b", "c"])

        (ref,) = discover_sessions()

        assert ref.record_count == 3
        assert ref.project_dir == encode_project_dir(project)
        assert ref.path.name == "s1.jsonl"

    def test_subagent_transcripts_belong_to_their_session(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """Sidechain work is part of the session's cost, so it is part of its coverage."""
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a", "b"])
        subagents = path.parent / "s1" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-1.jsonl").write_text(f"{_record('x')}\n", encoding="utf-8")

        (ref,) = discover_sessions()

        assert ref.subagent_paths == (subagents / "agent-1.jsonl",)
        assert ref.record_count == 3
        assert ref.files == (path, subagents / "agent-1.jsonl")

    def test_discovery_never_writes_to_the_transcript_tree(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """FR-020: user session records are read-only — no file created, changed, or touched."""
        projects = claude_config / "projects"
        _write_session(projects, tmp_path / "alpha", "s1", ["a", "b"])
        _write_session(projects, tmp_path / "beta", "s2", ["c"])
        before = _snapshot(claude_config)

        discover_sessions()
        sessions_for_cwd(tmp_path / "alpha")

        assert _snapshot(claude_config) == before


class TestSessionsForCwd:
    """FR-048, amended 2026-08-12.

    This used to be `TestLatestSessionForCwd`, pinning a zero-argument default of *one* session.
    The spec now defaults to the whole project, so the old assertions described a contract that
    no longer exists — they are rewritten here rather than deleted: the walk-up rule and the
    newest-first order they fenced are unchanged and still fenced.
    """

    def test_returns_every_session_of_the_current_project_newest_first(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        projects = claude_config / "projects"
        older = _write_session(projects, tmp_path / "alpha", "old", ["a"])
        newer = _write_session(projects, tmp_path / "alpha", "new", ["b"])
        _write_session(projects, tmp_path / "beta", "other", ["c"])
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

        refs = sessions_for_cwd(tmp_path / "alpha")

        # Newest first, and another project's session is not this project's business.
        assert [ref.session_id for ref in refs] == ["new", "old"]

    def test_a_subdirectory_resolves_to_the_project_that_owns_it(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        project = tmp_path / "alpha"
        nested = project / "src" / "pkg"
        nested.mkdir(parents=True)
        _write_session(claude_config / "projects", project, "s1", ["a"])

        assert [ref.session_id for ref in sessions_for_cwd(nested)] == ["s1"]

    def test_the_nearest_recorded_ancestor_wins_outright(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """A repo inside a recorded directory is its own project, not part of the parent's."""
        projects = claude_config / "projects"
        parent = tmp_path / "workspace"
        child = parent / "repo"
        child.mkdir(parents=True)
        _write_session(projects, parent, "parent-session", ["a"])
        _write_session(projects, child, "child-session", ["b"])

        assert [ref.session_id for ref in sessions_for_cwd(child)] == ["child-session"]

    def test_no_recorded_session_returns_nothing(self, claude_config: Path, tmp_path: Path) -> None:
        """Nothing found is an answer, not an error."""
        assert sessions_for_cwd(tmp_path / "unrecorded") == []


class TestCoverageFingerprint:
    def test_is_the_three_documented_components(self, claude_config: Path, tmp_path: Path) -> None:
        """``(record_count, last_record_uuid, byte_size)`` — research §3, data-model F1."""
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a", "b"])

        fingerprint = fingerprint_transcript(path)

        assert fingerprint.record_count == 2
        assert fingerprint.last_record_uuid == "b"
        assert fingerprint.byte_size == path.stat().st_size
        assert str(fingerprint) == f"2:b:{path.stat().st_size}"

    def test_is_stable_while_the_session_does_not_advance(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a", "b"])

        assert fingerprint_transcript(path) == fingerprint_transcript(path)

    def test_changes_when_the_session_advances_by_one_record(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """This is the detection FR-084 rests on: a stored result stops matching."""
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a", "b"])
        before = fingerprint_transcript(path)

        _append_record(path, "c")
        after = fingerprint_transcript(path)

        assert after != before
        assert after.record_count == 3
        assert after.last_record_uuid == "c"

    def test_appending_to_an_in_progress_session_is_detected(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """A live session moves under us; every appended turn must change the fingerprint."""
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a"])
        seen = {str(fingerprint_transcript(path))}

        for uuid in ("b", "c", "d"):
            _append_record(path, uuid)
            seen.add(str(fingerprint_transcript(path)))

        assert len(seen) == 4

    def test_touching_the_file_does_not_change_the_fingerprint(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """Deliberately not mtime: a touch or a restore must not look like new content."""
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a", "b"])
        before = fingerprint_transcript(path)

        os.utime(path, (9_000_000, 9_000_000))

        assert fingerprint_transcript(path) == before

    def test_a_subagent_appending_moves_the_session_fingerprint(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """Sidechain cost is session cost, so sidechain growth is session advancement."""
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a"])
        subagents = path.parent / "s1" / "subagents"
        subagents.mkdir(parents=True)
        agent = subagents / "agent-1.jsonl"
        agent.write_text(f"{_record('x')}\n", encoding="utf-8")
        before = session_ref(path).fingerprint

        _append_record(agent, "y")

        assert session_ref(path).fingerprint != before

    def test_an_empty_transcript_fingerprints_without_erroring(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")

        fingerprint = fingerprint_transcript(path)

        assert fingerprint.record_count == 0
        assert fingerprint.last_record_uuid is None
        assert fingerprint.byte_size == 0


class TestInProgressHint:
    def test_a_recently_written_session_is_flagged_in_progress(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a"])
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))

        assert session_ref(path, now=NOW + timedelta(seconds=30)).in_progress

    def test_an_old_session_is_not(self, claude_config: Path, tmp_path: Path) -> None:
        path = _write_session(claude_config / "projects", tmp_path / "alpha", "s1", ["a"])
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))

        assert not session_ref(
            path, now=NOW + IN_PROGRESS_WINDOW + timedelta(seconds=1)
        ).in_progress

    def test_a_missing_transcript_raises_with_its_path(self, tmp_path: Path) -> None:
        with pytest.raises(TranscriptTreeError, match="not a transcript file"):
            session_ref(tmp_path / "nope.jsonl")


class TestSessionNames:
    """A session is identified by what it was about, not by a UUID.

    Claude Code records a name for most sessions, and a listing of nine hundred UUIDs gives a
    reader no way to find the one they mean. So the name leads and enough of the id to select
    it follows — in that order, on every surface, from one function.
    """

    def test_the_generated_name_is_read(self, tmp_path: Path) -> None:
        assert session_title({"type": "ai-title", "aiTitle": "Add money file to plan"}) == (
            "Add money file to plan"
        )

    def test_a_name_the_user_set_wins(self) -> None:
        """`custom-title` is written after the generated one, and is the reader's own word."""
        record = {"type": "custom-title", "customTitle": "mine", "aiTitle": "generated"}
        assert session_title(record) == "mine"

    def test_a_record_that_is_not_a_title_yields_nothing(self) -> None:
        assert session_title({"type": "assistant", "aiTitle": "not a title record"}) is None

    def test_a_blank_name_is_not_a_name(self) -> None:
        assert session_title({"type": "ai-title", "aiTitle": "   "}) is None

    def test_the_name_is_found_on_the_pass_that_fingerprints(self, tmp_path: Path) -> None:
        """Reading the transcript twice would double the cost of listing a corpus."""
        path = tmp_path / "s.jsonl"
        path.write_text(
            '{"type": "ai-title", "aiTitle": "Name it", "sessionId": "s"}\n'
            '{"type": "user", "uuid": "u1"}\n',
            encoding="utf-8",
        )
        fingerprint, title = scan_transcript(path)
        assert title == "Name it"
        assert fingerprint.record_count == 2

    def test_an_unnamed_session_falls_back_to_its_id(self, tmp_path: Path) -> None:
        """Never an invented name — the id fragment is the honest answer."""
        path = tmp_path / "abcdef01-2345-6789-abcd-ef0123456789.jsonl"
        path.write_text('{"type": "user", "uuid": "u1"}\n', encoding="utf-8")
        reference = session_ref(path)
        assert reference.title is None
        assert reference.display_name == "abcdef01"

    def test_a_named_session_shows_the_name_then_the_id(self, tmp_path: Path) -> None:
        path = tmp_path / "abcdef01-2345-6789-abcd-ef0123456789.jsonl"
        path.write_text(
            '{"type": "ai-title", "aiTitle": "Fix the parser"}\n{"type": "user", "uuid": "u1"}\n',
            encoding="utf-8",
        )
        assert session_ref(path).display_name == "Fix the parser (abcdef01)"

    def test_the_short_id_is_the_first_uuid_block(self, tmp_path: Path) -> None:
        """Eight hex digits against a few hundred sessions: identification, not a hash."""
        path = tmp_path / "abcdef01-2345-6789-abcd-ef0123456789.jsonl"
        path.write_text('{"type": "user", "uuid": "u1"}\n', encoding="utf-8")
        assert session_ref(path).short_id == "abcdef01"
        assert len(session_ref(path).short_id) == SHORT_ID_LENGTH

    def test_an_over_long_name_is_clipped_at_a_word(self) -> None:
        """Claude Code takes the name from the conversation, so a pasted paragraph can be one.

        One such session was enough to blow the scope line back out into the wall of text that
        naming sessions was meant to replace.
        """
        pasted = "I have a problem I want to solve and here is a great deal of detail " * 3
        clipped = session_title({"type": "ai-title", "aiTitle": pasted})
        assert clipped is not None
        assert len(clipped) <= MAX_TITLE_LENGTH + 1
        assert clipped.endswith("…")
        assert not clipped.rstrip("…").endswith(" ")

    def test_a_name_is_collapsed_to_one_line(self) -> None:
        """A newline in a name breaks every single-line surface it appears on."""
        assert session_title({"type": "ai-title", "aiTitle": "two\n\n  lines"}) == "two lines"


class TestARelativeProjectIsResolvedBeforeItIsLookedUp:
    """`--project .` is an ordinary thing to type, and it encoded to `-`.

    A regression this branch introduced. Under the old separator-only rule `.` encoded to `.`,
    so the lookup landed on the projects *root*, whose glob matches nothing — an empty result,
    and harmless. Under the measured rule `.` encodes to `-`, which is the directory a POSIX
    machine records sessions run from `/`. So the lookup landed on a stranger's session and
    reported it as this project's cost: the same phantom the encoding fix exists to stop,
    reached from a third direction.

    Resolution happens at the lookup, deliberately not in `encode_project_dir` — see
    `TestALookupPathIsNotAnEncoding` below.
    """

    def test_a_dot_project_does_not_pick_up_another_machines_root_directory(
        self, claude_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projects = claude_config / "projects"
        posix_root = projects / "-"
        posix_root.mkdir()
        (posix_root / "someone-elses.jsonl").write_text(f"{_record('a')}\n", encoding="utf-8")
        work = tmp_path / "repo"
        work.mkdir()
        monkeypatch.chdir(work)

        assert sessions_for_project(Path(".")) == []

    def test_a_dot_project_finds_the_current_directory_s_own_sessions(
        self, claude_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: resolving must find the right answer, not merely avoid a wrong one."""
        work = tmp_path / "repo"
        work.mkdir()
        _write_session(claude_config / "projects", work.resolve(), "mine", ["a"])
        monkeypatch.chdir(work)

        assert [ref.session_id for ref in sessions_for_project(Path("."))] == ["mine"]

    def test_a_relative_subdirectory_resolves_against_the_working_directory(
        self, claude_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = tmp_path / "repo"
        (work / "sub").mkdir(parents=True)
        _write_session(claude_config / "projects", (work / "sub").resolve(), "nested", ["a"])
        monkeypatch.chdir(work)

        assert [ref.session_id for ref in sessions_for_project(Path("sub"))] == ["nested"]


class TestALookupPathIsNotAnEncoding:
    """Resolving belongs to the lookup, never to the encoder.

    `encode_project_dir` answers "what did the machine that recorded this session call it",
    which is a question about the path *string*. `~/.claude` is synced between machines, so
    asking a Windows host about a POSIX-recorded project is a real question with a real answer —
    and resolving that path first would anchor it to this host and encode a name nobody wrote.
    """

    def test_a_relative_path_is_made_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        assert project_lookup_path(Path(".")) == tmp_path.resolve()

    @pytest.mark.parametrize("anchored", ["/Users/x/p", "C:/Users/x/p", r"C:\Users\x\p"])
    def test_an_anchored_path_is_left_exactly_as_given(self, anchored: str) -> None:
        """`Path.is_absolute` is the wrong test here, and wrong in both directions.

        `/Users/x/p` reports False on Windows, and `C:/Users/x/p` reports False on POSIX. Either
        answer, acted on, resolves a foreign path onto this host and looks up the wrong name.
        """
        assert project_lookup_path(Path(anchored)) == Path(anchored)

    def test_the_encoder_itself_never_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It must keep answering about the string, whatever the working directory is."""
        monkeypatch.chdir(tmp_path)

        assert encode_project_dir(Path("C:/Users/x/p")) == "C--Users-x-p"
        assert encode_project_dir(Path("/Users/x/p")) == "-Users-x-p"


class TestADoubledDashReadsTwoWays:
    r"""`C--Users-x--claude` is how `C:\Users\x\.claude` is stored, and how `C:\Users\x\claude` is.

    Reading it naively drops the dot. On a machine where the undotted directory happens to
    exist, `must_exist=True` handed back a confidently wrong project path. Lookup always
    encodes, so this could never *select* a stranger's session — it could only mislabel the
    project column, which is still a number-adjacent surface telling the reader something false.
    """

    def test_both_readings_are_offered_naive_first(self) -> None:
        assert _readings("Users-x--claude") == ["Users/x/claude", "Users/x/.claude"]

    def test_a_name_without_a_doubled_dash_has_one_reading(self) -> None:
        assert _readings("Users-x-src") == ["Users/x/src"]

    def test_readings_stay_bounded(self) -> None:
        """Each doubled dash doubles the set, so a pathological name falls back to the naive one."""
        pathological = "a--b--c--d--e--f--g--h--i"

        assert _readings(pathological) == ["a/b/c/d/e/f/g/h/i"]

    def _present(self, monkeypatch: pytest.MonkeyPatch, *paths: Path) -> None:
        """Stand in for the filesystem.

        Faked rather than created, because the tie has to be observed at an *absolute* path and
        `tmp_path` cannot provide one: pytest's own temporary directory names contain hyphens,
        so the naive decode of any name built from it misses the whole prefix and returns None
        for a reason that has nothing to do with the rule under test. A test that passed that
        way would be fencing nothing.
        """
        existing = set(paths)
        monkeypatch.setattr(Path, "is_dir", lambda self: self in existing)

    def test_two_readings_that_both_exist_decode_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A coin toss presented as a project path is the wrong kind of confident."""
        self._present(monkeypatch, Path("C:/Users/x/claude"), Path("C:/Users/x/.claude"))

        assert decode_project_dir("C--Users-x--claude") is None

    def test_the_dotted_reading_is_returned_when_it_is_the_only_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And the rule pays for itself: `~/.claude` projects now decode instead of giving up."""
        self._present(monkeypatch, Path("C:/Users/x/.claude"))

        assert decode_project_dir("C--Users-x--claude") == Path("C:/Users/x/.claude")

    def test_the_naive_reading_still_wins_when_it_is_the_only_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._present(monkeypatch, Path("C:/Users/x/claude"))

        assert decode_project_dir("C--Users-x--claude") == Path("C:/Users/x/claude")


class TestDecodingIsMemoised:
    """One decode per project directory, not one per transcript.

    `session_ref` decodes for every transcript it builds, so a project holding two hundred
    sessions repeated the same reading — and the same `is_dir` calls — two hundred times. On a
    real 1,875-session corpus that measured 0.28s of stat calls, against 0.008s memoised.
    """

    def test_a_repeated_name_is_answered_from_the_memo(self) -> None:
        decode_project_dir.cache_clear()

        decode_project_dir("-not-a-real-path-98765")
        decode_project_dir("-not-a-real-path-98765")

        assert decode_project_dir.cache_info().hits == 1

    def test_the_memo_can_be_emptied(self) -> None:
        """The suite empties it between tests; without that a faked filesystem would leak."""
        decode_project_dir("-not-a-real-path-98765")

        decode_project_dir.cache_clear()

        assert decode_project_dir.cache_info().currsize == 0
