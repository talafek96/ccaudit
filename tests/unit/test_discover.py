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

from ccaudit.ingest.discover import (
    IN_PROGRESS_WINDOW,
    SHORT_ID_LENGTH,
    TranscriptTreeError,
    claude_home,
    decode_project_dir,
    discover_sessions,
    encode_project_dir,
    fingerprint_transcript,
    latest_session_for_cwd,
    scan_transcript,
    session_ref,
    session_title,
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
    def test_encoding_a_path_matches_the_claude_code_form(self) -> None:
        assert encode_project_dir(Path("/Users/x/projects/ccaudit")) == "-Users-x-projects-ccaudit"

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
        latest_session_for_cwd(tmp_path / "alpha")

        assert _snapshot(claude_config) == before


class TestLatestSessionForCwd:
    def test_returns_the_most_recent_session_of_the_current_project(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        """The zero-argument invocation's default (FR-048)."""
        projects = claude_config / "projects"
        older = _write_session(projects, tmp_path / "alpha", "old", ["a"])
        newer = _write_session(projects, tmp_path / "alpha", "new", ["b"])
        _write_session(projects, tmp_path / "beta", "other", ["c"])
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

        ref = latest_session_for_cwd(tmp_path / "alpha")

        assert ref is not None
        assert ref.session_id == "new"

    def test_a_subdirectory_resolves_to_the_project_that_owns_it(
        self, claude_config: Path, tmp_path: Path
    ) -> None:
        project = tmp_path / "alpha"
        nested = project / "src" / "pkg"
        nested.mkdir(parents=True)
        _write_session(claude_config / "projects", project, "s1", ["a"])

        ref = latest_session_for_cwd(nested)

        assert ref is not None
        assert ref.session_id == "s1"

    def test_no_recorded_session_returns_none(self, claude_config: Path, tmp_path: Path) -> None:
        """Nothing found is an answer, not an error."""
        assert latest_session_for_cwd(tmp_path / "unrecorded") is None


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
