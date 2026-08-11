"""Find session transcripts, and tell cheaply whether one has moved since we last looked.

Two jobs, both strictly read-only over ``~/.claude`` (FR-020) — this module opens files and
stats them, and never creates, writes, moves, or deletes anything under there.

**Discovery.** Transcripts live at ``<claude home>/projects/<encoded project path>/<session
id>.jsonl``, with a sibling ``<session id>/subagents/*.jsonl`` directory holding the sidechain
transcripts of any subagents that session spawned. All of those files are part of the same
session's cost and are carried together on one :class:`SessionRef`.

**Freshness.** A session's coverage fingerprint is ``(record_count, last_record_uuid,
byte_size)`` (research §3, data-model invariant F1). A stored result is current only while the
fingerprint it covered still matches. That makes staleness *observed* rather than assumed,
which is what lets FR-084 hold for a session that is still in progress while we report on it.

Deliberately **not mtime**: a touch, a restore, or a copy changes mtime without changing a
single record, and mtime is too coarse to notice a turn appended within the same second.
Deliberately **not a content hash** either — that reads every byte of every session on every
invocation and blows the corpus-wide budget (SC-021). The fingerprint costs one line-oriented
pass and no interpretation of the records.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ccaudit.ingest.records import iter_records

CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
PROJECTS_DIRNAME = "projects"
SUBAGENTS_DIRNAME = "subagents"
TRANSCRIPT_SUFFIX = ".jsonl"

# A session whose files were written this recently is *probably* still running. This is a hint
# for the listing only — it is never what decides whether a stored figure is current, because
# mtime is exactly the wrong signal for that (see module docstring). The fingerprint decides.
IN_PROGRESS_WINDOW = timedelta(minutes=5)


class TranscriptTreeError(RuntimeError):
    """The configured Claude home exists but is not usable. Carries the offending path."""


@dataclass(frozen=True)
class Fingerprint:
    """A session's coverage identity — what a stored result says it covered.

    ``record_count`` and ``byte_size`` span the session transcript *and* its subagent
    transcripts, so a subagent appending a record moves the fingerprint even when the main
    transcript is momentarily unchanged. ``last_record_uuid`` is the main transcript's final
    record, which is where the session's own conversation has reached.
    """

    record_count: int
    last_record_uuid: str | None
    byte_size: int

    def __str__(self) -> str:
        """Stable text form — this is what gets stored and compared (data-model F1/F2)."""
        return f"{self.record_count}:{self.last_record_uuid or '-'}:{self.byte_size}"


@dataclass(frozen=True)
class SessionRef:
    """One session on disk: where it is, what project it belongs to, and where it has got to.

    Everything a ``ccaudit sessions`` listing needs (FR-060) without parsing the transcript.
    """

    session_id: str
    path: Path
    project_dir: str
    project_path: Path | None
    fingerprint: Fingerprint
    modified_at: datetime
    in_progress: bool
    subagent_paths: tuple[Path, ...] = ()

    @property
    def record_count(self) -> int:
        """Records covered, session plus subagents. One source — the fingerprint (Principle IX)."""
        return self.fingerprint.record_count

    @property
    def byte_size(self) -> int:
        return self.fingerprint.byte_size

    @property
    def files(self) -> tuple[Path, ...]:
        """Every transcript file belonging to this session, in fingerprint order."""
        return (self.path, *self.subagent_paths)


def claude_home() -> Path:
    """The Claude Code configuration directory: ``$CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``."""
    override = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def projects_root(home: Path | None = None) -> Path:
    """The directory holding one subdirectory per project. May not exist; callers handle that."""
    return (home or claude_home()) / PROJECTS_DIRNAME


def encode_project_dir(project_path: Path) -> str:
    """Encode a filesystem path the way Claude Code names its project directories.

    This direction is exact: ``/Users/x/projects/ccaudit`` →
    ``-Users-x-projects-ccaudit``. Decoding is not (see :func:`decode_project_dir`), so
    resolving "which directory holds this project" always encodes rather than decodes.
    """
    return str(project_path).replace(os.sep, "-")


def decode_project_dir(directory_name: str, *, must_exist: bool = True) -> Path | None:
    """Best-effort recovery of the project path from a project directory's name.

    **The encoding is lossy.** Every ``/`` becomes ``-``, and ``-`` is a legal character in a
    directory name, so ``-a-b-my-project`` could be ``/a/b/my-project`` or ``/a/b/my/project``
    — nothing in the name distinguishes them. We therefore decode naively and, by default,
    only return a path that exists on this filesystem; otherwise we return ``None`` and the
    caller shows the encoded name rather than a confidently wrong path (Principle X).

    A project directory that has since been deleted or renamed decodes to ``None``. That is
    the honest answer: its transcripts are still analysable, we just cannot say where the code
    lived.
    """
    if not directory_name.startswith("-"):
        # Not the encoded form (a relative path, or a directory we did not create). Left alone.
        return None
    candidate = Path(directory_name.replace("-", os.sep))
    if not must_exist:
        return candidate
    return candidate if candidate.is_dir() else None


def fingerprint_transcript(path: Path, subagent_paths: tuple[Path, ...] = ()) -> Fingerprint:
    """Compute a session's coverage fingerprint. Read-only; no full parse, no interpretation.

    One line-oriented pass per file: records are counted and the last one's ``uuid`` is read.
    Nothing is typed, priced, or attributed here — that is what keeps it cheap enough to run
    over the whole corpus on every invocation (SC-021).
    """
    record_count, last_uuid = _count_records(path)
    byte_size = _byte_size(path)
    for subagent_path in subagent_paths:
        extra_count, _ = _count_records(subagent_path)
        record_count += extra_count
        byte_size += _byte_size(subagent_path)
    return Fingerprint(record_count=record_count, last_record_uuid=last_uuid, byte_size=byte_size)


def session_ref(path: Path, *, now: datetime | None = None) -> SessionRef:
    """Build a :class:`SessionRef` for one transcript file. Raises if it is not a file."""
    if not path.is_file():
        raise TranscriptTreeError(f"not a transcript file: {path}")

    subagent_paths = _subagent_paths(path)
    fingerprint = fingerprint_transcript(path, subagent_paths)
    modified_at = max(_modified_at(candidate) for candidate in (path, *subagent_paths))
    project_dir = path.parent.name
    return SessionRef(
        # The file name is the session identity on disk. Records inside a *resumed* transcript
        # can still carry the original session's id, so the name is the stable one to list by.
        session_id=path.stem,
        path=path,
        project_dir=project_dir,
        project_path=decode_project_dir(project_dir),
        fingerprint=fingerprint,
        modified_at=modified_at,
        in_progress=(now or datetime.now(UTC)) - modified_at < IN_PROGRESS_WINDOW,
        subagent_paths=subagent_paths,
    )


def discover_sessions(home: Path | None = None, *, now: datetime | None = None) -> list[SessionRef]:
    """Every session transcript under the Claude home, newest first.

    An absent or empty tree is a normal outcome, not a failure: the result is an empty list
    (there genuinely are no sessions). A ``projects`` path that exists but is not a directory
    *is* a broken invariant and raises.
    """
    root = projects_root(home)
    if not root.exists():
        return []
    if not root.is_dir():
        raise TranscriptTreeError(f"expected a directory of projects, found a file: {root}")

    sessions = [
        session_ref(path, now=now)
        for project_dir in sorted(root.iterdir())
        if project_dir.is_dir()
        for path in sorted(project_dir.glob(f"*{TRANSCRIPT_SUFFIX}"))
        if path.is_file()
    ]
    # Newest first for display; the path breaks ties so repeated runs order identically even
    # when two transcripts share an mtime (FR-017, SC-009).
    sessions.sort(key=lambda ref: (ref.modified_at, str(ref.path)), reverse=True)
    return sessions


def sessions_for_project(
    project_path: Path, home: Path | None = None, *, now: datetime | None = None
) -> list[SessionRef]:
    """Sessions recorded for one project directory, newest first. Empty when it has none."""
    directory = projects_root(home) / encode_project_dir(project_path)
    if not directory.is_dir():
        return []
    sessions = [
        session_ref(path, now=now)
        for path in sorted(directory.glob(f"*{TRANSCRIPT_SUFFIX}"))
        if path.is_file()
    ]
    sessions.sort(key=lambda ref: (ref.modified_at, str(ref.path)), reverse=True)
    return sessions


def latest_session_for_cwd(
    cwd: Path | None = None, home: Path | None = None, *, now: datetime | None = None
) -> SessionRef | None:
    """The most recent session of the project containing ``cwd`` — the zero-argument default.

    Walks up from ``cwd``: running ``ccaudit`` from ``repo/src/pkg`` should find the session
    Claude Code recorded for ``repo``. Returns ``None`` when no ancestor has any transcripts,
    which is a normal outcome the caller reports rather than an error (FR-048).
    """
    start = (cwd or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        sessions = sessions_for_project(directory, home, now=now)
        if sessions:
            return sessions[0]
    return None


def _subagent_paths(path: Path) -> tuple[Path, ...]:
    """Sidechain transcripts of the subagents this session spawned, if any.

    Sorted, because the fingerprint is built from them and must not depend on directory order.
    """
    subagents = path.parent / path.stem / SUBAGENTS_DIRNAME
    if not subagents.is_dir():
        return ()
    return tuple(sorted(p for p in subagents.glob(f"*{TRANSCRIPT_SUFFIX}") if p.is_file()))


def _count_records(path: Path) -> tuple[int, str | None]:
    """Well-formed record count, and the uuid of the last record that carries one.

    Goes through the shared :func:`iter_records` so there is exactly one JSON reading path in
    the ingest layer — a second, faster-but-different scanner would eventually disagree with
    the parser about what counts as a record (Principle II).
    """
    count = 0
    last_uuid: str | None = None
    for _, record in iter_records(path):
        count += 1
        uuid = record.get("uuid")
        if isinstance(uuid, str) and uuid:
            last_uuid = uuid
    return count, last_uuid


def _byte_size(path: Path) -> int:
    return path.stat().st_size


def _modified_at(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
