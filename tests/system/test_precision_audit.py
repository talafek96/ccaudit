"""System test — precision and uncertainty, audited across every surface (SC-036, SC-037).

Principle X's least obvious clause: **exactness is not accuracy**. The arithmetic conserves to
the micro-dollar, but the inputs are imputed prices and a splitting policy. A figure printed to
the cent claims a precision it does not have, and a reader who cannot tell which figures are
measured and which rest on a policy choice will over-trust the whole table.

So this audit checks the *presentation* rules, on every surface a user can see, rather than any
single component in isolation.
"""

import json
import re
from pathlib import Path

import pytest

from ccaudit.cli import EXIT_OK, main
from ccaudit.config import sig_figs_for
from ccaudit.money import format_micros
from tests.fixtures.builder import TranscriptBuilder

pytestmark = pytest.mark.system


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude"
    builder = TranscriptBuilder(session_id="audit-1", project_path="/repo/alpha")
    builder.add_turn(
        input_tokens=400, cache_creation_1h=9_000, output_tokens=250, tool_use_ids=("t1",)
    )
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/alpha/src/big.py", text="x" * 40_000)
    builder.add_turn(input_tokens=30, cache_creation_5m=4_000, cache_read=9_200, output_tokens=90)
    builder.add_at_mention(display_path="/repo/alpha/CLAUDE.md", content="# Rules\n" * 500)
    builder.add_turn(input_tokens=12, cache_read=13_300, output_tokens=60)
    builder.add_turn(input_tokens=6, cache_read=13_300, output_tokens=40)
    builder.write_to_project_tree(home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CCAUDIT_HOME", str(tmp_path / "state"))
    return home


def payload(capsys: pytest.CaptureFixture[str], *args: str) -> dict:
    assert main(["--all", "--json", *args]) == EXIT_OK
    return json.loads(capsys.readouterr().out)


def terminal(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    assert main(["--all", *args]) == EXIT_OK
    return capsys.readouterr().out


class TestPrecisionFollowsConfidence:
    def test_every_item_declares_a_precision_matching_its_confidence(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for item in payload(capsys)["items"]:
            assert item["display_sig_figs"] == sig_figs_for(item["confidence"])

    def test_a_policy_dependent_figure_is_never_offered_at_full_precision(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Carry rests on a splitting policy the reader can change. It is not known to the cent."""
        carried = [item for item in payload(capsys)["items"] if item["carry_micros"]]
        assert carried
        assert all(item["display_sig_figs"] <= 2 for item in carried)

    def test_the_terminal_prints_each_figure_at_its_own_precision(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = payload(capsys)
        text = terminal(capsys)
        for item in data["items"]:
            expected = format_micros(item["total_micros"], item["display_sig_figs"])
            assert expected in text, f"{item['display']} should print as {expected}"

    def test_no_figure_is_printed_finer_than_its_confidence_supports(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The failure this guards: a policy-derived number rendered to the cent."""
        data = payload(capsys)
        text = terminal(capsys)
        for item in data["items"]:
            coarse = format_micros(item["total_micros"], item["display_sig_figs"])
            precise = format_micros(item["total_micros"], 6)
            if precise != coarse:
                assert precise not in text, f"{item['display']} leaked full precision"


class TestUncertaintyIsExpressed:
    def test_every_item_names_what_dominates_its_range(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-096 — express the uncertainty, do not merely label it."""
        for item in payload(capsys)["items"]:
            uncertainty = item["uncertainty"]
            assert uncertainty["driver"]
            assert uncertainty["low_micros"] <= item["total_micros"] <= uncertainty["high_micros"]

    def test_the_totals_name_their_dominant_uncertainties(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-097 — the three things that dominate, stated wherever totals appear."""
        notes = " ".join(payload(capsys)["totals"]["uncertainty_notes"]).lower()
        assert "imputed" in notes or "list price" in notes
        assert "policy" in notes
        assert "absent" in notes or "stripped" in notes

    def test_the_terminal_tells_a_reader_how_wrong_this_could_be(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SC-037 — the answer is on the same surface as the figure, not in a doc."""
        text = terminal(capsys).lower()
        assert "estimate" in text
        assert "policy" in text or "divided" in text


class TestCostBasisEverywhere:
    def test_no_surface_claims_an_amount_was_billed(
        self, corpus: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """FR-010. The word may appear only inside the disclaimer that denies it."""
        surfaces = [terminal(capsys)]
        assert main(["--all", "--json"]) == EXIT_OK
        surfaces.append(capsys.readouterr().out)
        out = tmp_path / "report.html"
        assert main(["report", "--all", "--out", str(out)]) == EXIT_OK
        capsys.readouterr()
        surfaces.append(out.read_text(encoding="utf-8"))

        for surface in surfaces:
            for match in re.finditer(r"billed", surface):
                start = max(0, match.start() - 4)
                assert surface[start : match.start()] == "not ", surface[start - 60 : match.end()]

    def test_the_payload_declares_the_basis_explicitly(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert payload(capsys)["cost_basis"] == "api_equivalent_estimate"


class TestSharesAccompanyAbsolutes:
    def test_every_item_carries_a_share(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FR-011 — the share survives being wrong about pricing; the dollars do not."""
        for item in payload(capsys)["items"]:
            assert 0.0 <= item["share"] <= 1.0

    def test_the_shares_sum_to_the_attributed_share_of_the_total(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = payload(capsys)
        totals = data["totals"]
        item_share = sum(item["share"] for item in data["items"])
        expected = sum(item["total_micros"] for item in data["items"]) / totals["cost_micros"]
        assert item_share == pytest.approx(expected, abs=1e-6)
