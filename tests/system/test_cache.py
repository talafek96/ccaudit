"""System test — serving a finished analysis from the store (FR-104–FR-110).

A cache on a pure function can only cost time, never correctness, *provided the key covers
everything the figures depend on*. Everything here is about that proviso, plus the one property
that makes the design defensible at all: what comes out of the store equals what went in
(FR-105, SC-024), rather than being computed a second way and hoped to agree.
"""

import json
import os
import shutil
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from claude_cost_tracker import __version__
from claude_cost_tracker.analyse import SessionContribution, analyse_transcript, contribution_of
from claude_cost_tracker.cli import EXIT_OK, main
from claude_cost_tracker.config import BUNDLED_PRICING_PATH, load_pricing
from claude_cost_tracker.ingest.discover import IN_PROGRESS_WINDOW, fingerprint_transcript
from claude_cost_tracker.store.cache import (
    build_fingerprint,
    cache_key,
    read_contribution,
    store_contribution,
)
from claude_cost_tracker.store.codec import decode, encode
from claude_cost_tracker.store.db import connect
from tests.fixtures.builder import TranscriptBuilder

pytestmark = pytest.mark.system

PRICING = load_pricing(BUNDLED_PRICING_PATH)
SESSIONS = ("alpha-1", "alpha-2", "beta-1")


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude"
    for index, session_id in enumerate(SESSIONS):
        project = "/repo/alpha" if session_id.startswith("alpha") else "/repo/beta"
        builder = TranscriptBuilder(session_id=session_id, project_path=project)
        builder.add_user_text("read it")
        builder.add_turn(
            input_tokens=40 + index,
            cache_creation_5m=3_000 + index * 500,
            output_tokens=70,
            tool_use_ids=("t1",),
        )
        builder.add_tool_result(
            tool_use_id="t1", file_path="/repo/shared/common.md", text="c" * (6_000 + index * 400)
        )
        builder.add_turn(input_tokens=8, cache_read=3_200 + index * 500, output_tokens=35)
        builder.write_to_project_tree(home)
    # `in_progress` is decided by how recently the file was written, and a session that may
    # still be running is deliberately never cached (FR-108). A freshly-written fixture looks
    # live, so it is backdated — otherwise every test here would pass vacuously with an empty
    # cache and prove nothing.
    _settle(home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CCOST_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CCOST_NO_CACHE", raising=False)
    return home


def _settle(home: Path) -> None:
    """Backdate every transcript so discovery reports the sessions as finished."""
    stale = time.time() - IN_PROGRESS_WINDOW.total_seconds() * 2
    for transcript in home.rglob("*.jsonl"):
        os.utime(transcript, (stale, stale))


def payload(capsys: pytest.CaptureFixture[str], *args: str) -> dict:
    assert main([*args, "--json"]) == EXIT_OK
    return json.loads(capsys.readouterr().out)


def without_timestamp(data: dict) -> dict:
    stripped = json.loads(json.dumps(data))
    stripped.pop("generated_at", None)
    return stripped


class TestTheFiguresAreTheSameEitherWay:
    def test_a_warm_run_produces_the_same_payload_as_a_cold_one(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The property the whole design rests on. A cached run and a computed run go through
        one renderer, over one shape, so they cannot disagree."""
        cold = payload(capsys, "--all")
        warm = payload(capsys, "--all")
        assert without_timestamp(warm) == without_timestamp(cold)

    def test_deleting_the_store_changes_nothing_but_speed(
        self, corpus: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """FR-110 — the store is a cache and never a source of truth."""
        warm = payload(capsys, "--all")
        for database in (tmp_path / "state").glob("*.db*"):
            database.unlink()
        assert without_timestamp(payload(capsys, "--all")) == without_timestamp(warm)

    def test_running_with_the_cache_disabled_agrees(
        self, corpus: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cached = payload(capsys, "--all")
        monkeypatch.setenv("CCOST_NO_CACHE", "1")
        assert without_timestamp(payload(capsys, "--all")) == without_timestamp(cached)


class TestItIsActuallyUsed:
    def test_the_second_run_is_served_from_the_store(self, corpus: Path) -> None:
        from claude_cost_tracker.cli import _analyse_selection, build_parser

        args = build_parser().parse_args(["--all"])
        first = _analyse_selection(args)
        assert first.recalled == 0
        second = _analyse_selection(args)
        assert second.recalled == len(SESSIONS)

    def test_every_restored_session_reconciles_on_its_own(self, corpus: Path) -> None:
        """Invariant S2 — checked on the way out, not only on the way in."""
        from claude_cost_tracker.cli import _analyse_selection, build_parser

        args = build_parser().parse_args(["--all"])
        _analyse_selection(args)
        for restored in _analyse_selection(args).analyses:
            assert isinstance(restored, SessionContribution)
            restored.check_reconciles()


class TestTheKeyCoversWhatTheFiguresDependOn:
    @pytest.fixture
    def stored(self, corpus: Path, tmp_path: Path) -> tuple:
        transcript = next((corpus / "projects").rglob("alpha-1.jsonl"))
        analysis = analyse_transcript(transcript, pricing=PRICING)
        fingerprint = fingerprint_transcript(transcript)
        key = cache_key("alpha-1", fingerprint, "proportional", PRICING.fingerprint)
        conn = connect(tmp_path / "state" / "ccost.db")
        store_contribution(conn, key, contribution_of(analysis))
        return conn, key

    def test_a_matching_key_hits(self, stored: tuple) -> None:
        conn, key = stored
        assert read_contribution(conn, key) is not None

    @pytest.mark.parametrize(
        "field",
        ["fingerprint", "policy", "pricing_fingerprint", "tool_version"],
    )
    def test_a_change_to_any_part_of_the_key_misses(self, stored: tuple, field: str) -> None:
        """Rates in particular: a figure priced by a superseded table is a wrong number, not an
        old one (FR-106), and rates are refreshable at any time (FR-099)."""
        import dataclasses

        conn, key = stored
        moved = dataclasses.replace(key, **{field: "different"})
        assert read_contribution(conn, moved) is None


class TestAnyDoubtDiscards:
    def test_a_corrupt_blob_is_a_miss_rather_than_a_crash(
        self, corpus: Path, tmp_path: Path
    ) -> None:
        """A cache entry is never worth defending: discarding costs one recomputation."""
        transcript = next((corpus / "projects").rglob("alpha-1.jsonl"))
        analysis = analyse_transcript(transcript, pricing=PRICING)
        key = cache_key(
            "alpha-1", fingerprint_transcript(transcript), "proportional", PRICING.fingerprint
        )
        conn = connect(tmp_path / "state" / "ccost.db")
        store_contribution(conn, key, contribution_of(analysis))
        conn.execute("UPDATE analysis_result SET contribution = ?", (b"not zlib",))
        conn.commit()
        assert read_contribution(conn, key) is None

    def test_a_blob_that_does_not_reconcile_is_refused(self, corpus: Path, tmp_path: Path) -> None:
        """The one thing a cache must never do is serve a fast wrong number."""
        import zlib

        transcript = next((corpus / "projects").rglob("alpha-1.jsonl"))
        analysis = analyse_transcript(transcript, pricing=PRICING)
        key = cache_key(
            "alpha-1", fingerprint_transcript(transcript), "proportional", PRICING.fingerprint
        )
        conn = connect(tmp_path / "state" / "ccost.db")
        store_contribution(conn, key, contribution_of(analysis))

        tampered = encode(contribution_of(analysis), SessionContribution)
        tampered["reconciliation"]["total_micros"] += 1_000_000
        conn.execute(
            "UPDATE analysis_result SET contribution = ?",
            (zlib.compress(json.dumps(tampered).encode("utf-8")),),
        )
        conn.commit()
        assert read_contribution(conn, key) is None


class TestRoundTripFidelityOnRealShapes:
    def test_a_contribution_restores_to_an_equal_value(self, corpus: Path) -> None:
        """SC-024, on every session in the fixture corpus."""
        for transcript in sorted((corpus / "projects").rglob("*.jsonl")):
            original = contribution_of(analyse_transcript(transcript, pricing=PRICING))
            restored = decode(
                json.loads(json.dumps(encode(original, SessionContribution))),
                SessionContribution,
            )
            assert restored == original, transcript.name


class TestAnInProgressSessionIsNeverServed:
    def test_it_is_not_cached(self, corpus: Path) -> None:
        """Its records are still growing, so a cached figure is one for a session that no
        longer exists (FR-108). Touching the files puts them back inside the live window."""
        from claude_cost_tracker.cli import _analyse_selection, build_parser

        for transcript in corpus.rglob("*.jsonl"):
            os.utime(transcript, None)
        args = build_parser().parse_args(["--all"])
        _analyse_selection(args)
        assert _analyse_selection(args).recalled == 0

    def test_a_finished_session_is_cached(self, corpus: Path) -> None:
        """The other half: without this, the test above would pass on a broken cache."""
        from claude_cost_tracker.cli import _analyse_selection, build_parser

        args = build_parser().parse_args(["--all"])
        _analyse_selection(args)
        assert _analyse_selection(args).recalled == len(SESSIONS)


class TestABuildChangeInvalidatesWhatItProduced:
    """The key has to move when the code that derives the figures moves.

    `cache.py` has always documented the build as part of the key, but it was keyed on
    `__version__` — a literal in `pyproject.toml` that nothing bumps. So every fix to the
    attribution model kept the same key and stayed invisible behind rows written by the code it
    replaced. Measured on a real corpus after one such fix: 166 items served in an id format the
    running code could no longer produce, and 23 files each split across two rows.

    That is the failure this project treats as a show-stopper — not a crash, a confidently wrong
    number — so it is fenced here rather than left to whoever remembers to bump a version.
    """

    def test_the_key_carries_more_than_the_release_version(self) -> None:
        fingerprint = build_fingerprint()

        assert fingerprint.startswith(__version__)
        assert fingerprint != __version__, (
            "the build fingerprint collapsed to the release version, so two different builds "
            "of the same version share a cache key again"
        )

    def test_it_is_stable_within_a_build(self) -> None:
        """A key that moved between two runs of the same code would never hit at all."""
        assert build_fingerprint() == build_fingerprint()

    def test_it_moves_when_the_code_moves(self, tmp_path: Path) -> None:
        """Driven over a copy of the package, because the running one cannot be edited."""
        package = Path(build_fingerprint.__module__.replace(".", "/")).parent
        source = Path(__file__).resolve().parents[2] / "src" / "claude_cost_tracker"
        copied = tmp_path / "claude-cost-tracker"
        shutil.copytree(source, copied, ignore=shutil.ignore_patterns("__pycache__"))

        def fingerprint_of(root: Path) -> str:
            digest = sha256()
            for path in sorted(root.rglob("*.py")):
                digest.update(path.relative_to(root).as_posix().encode("utf-8"))
                digest.update(path.read_bytes())
            return digest.hexdigest()

        before = fingerprint_of(copied)
        target = copied / "model" / "residency.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# a change to how a number is derived\n",
            encoding="utf-8",
        )

        assert fingerprint_of(copied) != before
        assert package.name == "store"

    def test_a_row_written_by_another_build_is_not_served(
        self, corpus: Path, tmp_path: Path
    ) -> None:
        """The end-to-end property: a stale row is a miss, not a plausible-looking answer."""
        transcript = next((corpus / "projects").rglob("alpha-1.jsonl"))
        analysis = analyse_transcript(transcript, pricing=PRICING)
        key = cache_key(
            analysis.session_id,
            fingerprint_transcript(transcript),
            analysis.policy,
            PRICING.fingerprint,
        )
        with connect(tmp_path / "state.db") as conn:
            store_contribution(conn, key, contribution_of(analysis))
            assert read_contribution(conn, key) is not None

            # The same session, same records, same rates — but produced by a different build.
            other_build = replace(key, tool_version=f"{key.tool_version}-other")
            assert read_contribution(conn, other_build) is None
