"""System test — serving a finished analysis from the store (FR-104–FR-110).

A cache on a pure function can only cost time, never correctness, *provided the key covers
everything the figures depend on*. Everything here is about that proviso, plus the one property
that makes the design defensible at all: what comes out of the store equals what went in
(FR-105, SC-024), rather than being computed a second way and hoped to agree.
"""

import json
import os
import time
from pathlib import Path

import pytest

from ccaudit.analyse import SessionContribution, analyse_transcript, contribution_of
from ccaudit.cli import EXIT_OK, main
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing
from ccaudit.ingest.discover import IN_PROGRESS_WINDOW, fingerprint_transcript
from ccaudit.store.cache import cache_key, read_contribution, store_contribution
from ccaudit.store.codec import decode, encode
from ccaudit.store.db import connect
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
    monkeypatch.setenv("CCAUDIT_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CCAUDIT_NO_CACHE", raising=False)
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
        monkeypatch.setenv("CCAUDIT_NO_CACHE", "1")
        assert without_timestamp(payload(capsys, "--all")) == without_timestamp(cached)


class TestItIsActuallyUsed:
    def test_the_second_run_is_served_from_the_store(self, corpus: Path) -> None:
        from ccaudit.cli import _analyse_selection, build_parser

        args = build_parser().parse_args(["--all"])
        first = _analyse_selection(args)
        assert first.recalled == 0
        second = _analyse_selection(args)
        assert second.recalled == len(SESSIONS)

    def test_every_restored_session_reconciles_on_its_own(self, corpus: Path) -> None:
        """Invariant S2 — checked on the way out, not only on the way in."""
        from ccaudit.cli import _analyse_selection, build_parser

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
        conn = connect(tmp_path / "state" / "ccaudit.db")
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
        conn = connect(tmp_path / "state" / "ccaudit.db")
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
        conn = connect(tmp_path / "state" / "ccaudit.db")
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
        from ccaudit.cli import _analyse_selection, build_parser

        for transcript in corpus.rglob("*.jsonl"):
            os.utime(transcript, None)
        args = build_parser().parse_args(["--all"])
        _analyse_selection(args)
        assert _analyse_selection(args).recalled == 0

    def test_a_finished_session_is_cached(self, corpus: Path) -> None:
        """The other half: without this, the test above would pass on a broken cache."""
        from ccaudit.cli import _analyse_selection, build_parser

        args = build_parser().parse_args(["--all"])
        _analyse_selection(args)
        assert _analyse_selection(args).recalled == len(SESSIONS)
