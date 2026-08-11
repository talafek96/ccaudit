"""Contract on the report-data payload — the shape three surfaces all render.

These tests fence the honesty rules, not the formatting. Each one corresponds to a promise the
tool makes about its numbers: it adds up, the remainder is always visible, nothing is called an
amount charged, labels come from the registry, precision matches confidence, redaction removes
paths without removing the argument, and two runs produce the same bytes.
"""

import json
from pathlib import Path

import pytest

from ccaudit.analyse import SessionAnalysis, analyse_transcript
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing
from ccaudit.config.components import CHARGE_COMPONENTS, sig_figs_for
from ccaudit.model.reconcile import ReconciliationError
from ccaudit.render.data import SCHEMA_VERSION, build_report_data
from tests.fixtures.builder import TranscriptBuilder

PRICING = load_pricing(BUNDLED_PRICING_PATH)
FIXED_TIME = "2026-08-11T12:00:00+00:00"


def busy_session() -> TranscriptBuilder:
    """A session with the shapes that actually exercise attribution, not a happy path."""
    builder = TranscriptBuilder()
    builder.add_user_text("audit the config")
    builder.add_turn(
        input_tokens=420, cache_creation_1h=12_000, output_tokens=310, tool_use_ids=("t1",)
    )
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/src/app.py", text="x = 1\n" * 200)
    builder.add_ui_noise(4)
    builder.add_turn(input_tokens=37, cache_read=12_400, output_tokens=95, tool_use_ids=("t2",))
    builder.add_tool_result(
        tool_use_id="t2", file_path="/repo/docs/guide.md", text="# Guide\n" * 90
    )
    builder.add_at_mention(display_path="/repo/CLAUDE.md", content="# Rules\n" * 40)
    builder.add_turn(input_tokens=11, cache_creation_5m=3_100, cache_read=12_400, output_tokens=71)
    builder.add_turn(input_tokens=3, cache_read=15_500, output_tokens=44)
    return builder


def analyse(
    builder: TranscriptBuilder,
    tmp_path: Path,
    *,
    name: str = "s",
    policy: str = "proportional",
    provisional: bool = False,
) -> SessionAnalysis:
    return analyse_transcript(
        builder.write(tmp_path / f"{name}.jsonl"),
        pricing=PRICING,
        policy=policy,
        provisional=provisional,
    )


@pytest.fixture
def analysis(tmp_path: Path) -> SessionAnalysis:
    return analyse(busy_session(), tmp_path)


@pytest.fixture
def payload(analysis: SessionAnalysis) -> dict:
    return build_report_data([analysis], generated_at=FIXED_TIME)


class TestItAddsUp:
    """The core promise. A payload that fails this must never reach a consumer (SC-001)."""

    def test_attributed_plus_unattributed_equals_the_total_exactly(self, payload: dict) -> None:
        totals = payload["totals"]
        assert totals["attributed_micros"] + totals["unattributed_micros"] == totals["cost_micros"]

    def test_the_components_sum_to_the_total(self, payload: dict) -> None:
        assert (
            sum(c["cost_micros"] for c in payload["components"]) == payload["totals"]["cost_micros"]
        )

    def test_every_conclusion_plus_the_remainder_equals_the_total(self, payload: dict) -> None:
        """The form a reader can check by adding up a printed column."""
        concluded = sum(c["cost_micros"] for c in payload["attribution"])
        assert (
            concluded + payload["totals"]["unattributed_micros"] == payload["totals"]["cost_micros"]
        )

    def test_the_items_partition_the_item_level_conclusions(self, payload: dict) -> None:
        by_id = {c["id"]: c["cost_micros"] for c in payload["attribution"]}
        assert sum(i["direct_micros"] for i in payload["items"]) == by_id["direct"]
        assert sum(i["carry_micros"] for i in payload["items"]) == by_id["carry"]

    def test_output_cost_is_never_charged_to_an_item(self, payload: dict) -> None:
        """Invariant A2: what the model wrote is caused by the exchange, not by a file."""
        output = next(c for c in payload["attribution"] if c["id"] == "output")
        assert output["per_item"] is False
        assert output["cost_micros"] > 0

    def test_per_item_figures_never_exceed_what_was_attributed(self, payload: dict) -> None:
        items_total = sum(item["total_micros"] for item in payload["items"])
        assert items_total <= payload["totals"]["attributed_micros"]

    def test_it_refuses_to_produce_a_payload_that_does_not_add_up(
        self, analysis: SessionAnalysis
    ) -> None:
        """A corrupted breakdown raises rather than being handed to a renderer."""
        analysis.attribution.attributions.append(
            analysis.attribution.attributions[0].__class__(
                session_id=analysis.session_id,
                turn_index=0,
                target_kind="item",
                target_id=next(iter(analysis.timeline.items)),
                component="carry",
                cost_micros=analysis.total_micros * 10,
                basis="measured",
                confidence="medium",
            )
        )
        with pytest.raises(ReconciliationError):
            build_report_data([analysis], generated_at=FIXED_TIME)

    @pytest.mark.parametrize("policy", ["proportional", "exclusive"])
    def test_it_adds_up_under_either_policy(self, policy: str, tmp_path: Path) -> None:
        analysis = analyse(busy_session(), tmp_path, policy=policy)
        totals = build_report_data([analysis], generated_at=FIXED_TIME)["totals"]
        assert totals["attributed_micros"] + totals["unattributed_micros"] == totals["cost_micros"]

    def test_an_empty_session_still_produces_a_payload_that_adds_up(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_ui_noise(6)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)
        assert payload["totals"]["cost_micros"] == 0
        assert payload["totals"]["unattributed_share"] == 0.0


class TestHonestLabelling:
    def test_the_cost_basis_is_an_estimate_and_never_a_charged_amount(self, payload: dict) -> None:
        assert payload["cost_basis"] == "api_equivalent_estimate"
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["currency"] == "USD"

    def test_no_figure_is_described_as_an_amount_charged(self, payload: dict) -> None:
        """'billed' may appear only inside a denial that these figures are a bill."""
        text = json.dumps(payload)
        for index in _occurrences(text, "billed"):
            assert text[max(0, index - 4) : index] == "not ", text[index - 60 : index + 40]

    def test_the_unattributed_share_is_present_however_small(self, payload: dict) -> None:
        assert "unattributed_share" in payload["totals"]
        assert 0.0 <= payload["totals"]["unattributed_share"] <= 1.0

    def test_every_absolute_carries_a_share(self, payload: dict) -> None:
        for component in payload["components"]:
            assert "share" in component
        for item in payload["items"]:
            assert "share" in item

    def test_component_labels_come_from_the_registry(self, payload: dict) -> None:
        """A renderer that re-types a plain-language name is a defect (Principle IX)."""
        expected = {c.id: (c.technical_name, c.plain_name) for c in CHARGE_COMPONENTS}
        actual = {c["id"]: (c["technical_name"], c["plain_name"]) for c in payload["components"]}
        assert actual == expected

    def test_the_uncertainty_notes_name_the_three_dominant_uncertainties(
        self, payload: dict
    ) -> None:
        notes = " ".join(payload["totals"]["uncertainty_notes"]).lower()
        assert "api-equivalent" in notes  # imputed prices, not a bill
        assert "'proportional' policy" in notes  # the shared-carry split is a choice
        assert "stripped before the transcript" in notes  # content absent from the records

    def test_the_uncertainty_notes_say_each_thing_once(self, payload: dict) -> None:
        notes = payload["totals"]["uncertainty_notes"]
        assert len(notes) == len(set(notes))

    def test_the_policy_note_uses_the_policys_own_description(self, tmp_path: Path) -> None:
        analysis = analyse(busy_session(), tmp_path, policy="exclusive")
        notes = build_report_data([analysis], generated_at=FIXED_TIME)["totals"][
            "uncertainty_notes"
        ]
        assert any("only for carry cost it alone caused" in note for note in notes)

    def test_limitations_are_required_output(self, payload: dict) -> None:
        assert payload["diagnostics"]["limitations"]
        assert payload["diagnostics"]["unparseable_records"] == 0


class TestPrecisionMatchesConfidence:
    def test_every_item_carries_its_basis_confidence_and_precision(self, payload: dict) -> None:
        for item in payload["items"]:
            assert item["basis"] in ("exact", "measured", "estimated")
            assert item["confidence"] in ("high", "medium", "low")
            assert item["display_sig_figs"] == sig_figs_for(item["confidence"])

    def test_a_policy_dependent_figure_is_not_offered_at_full_precision(
        self, payload: dict
    ) -> None:
        """Carry rests on a splitting policy, so it never claims six figures of accuracy."""
        carried = [item for item in payload["items"] if item["carry_micros"]]
        assert carried
        assert all(item["display_sig_figs"] <= 2 for item in carried)

    def test_an_estimated_size_drops_the_precision_to_one_figure(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=50, cache_creation_5m=9_000, output_tokens=20)
        builder.add_tool_schema_delta(added=["mcp__demo__go"], added_lines=400)
        builder.add_turn(input_tokens=5, cache_read=9_400, output_tokens=15)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)

        estimated = [item for item in payload["items"] if item["basis"] == "estimated"]
        assert estimated
        assert all(item["display_sig_figs"] == 1 for item in estimated)
        assert all(item["uncertainty"]["driver"] == "size_estimate" for item in estimated)

    def test_every_item_names_what_dominates_its_range(self, payload: dict) -> None:
        for item in payload["items"]:
            uncertainty = item["uncertainty"]
            assert uncertainty["driver"] in (
                "size_estimate",
                "carry_split_policy",
                "turn_level_join",
            )
            assert uncertainty["low_micros"] <= item["total_micros"] <= uncertainty["high_micros"]
            assert uncertainty["low_micros"] >= 0


class TestItems:
    def test_items_are_ranked_most_expensive_first(self, payload: dict) -> None:
        totals = [item["total_micros"] for item in payload["items"]]
        assert totals == sorted(totals, reverse=True)

    def test_direct_and_carry_sum_to_the_item_total(self, payload: dict) -> None:
        for item in payload["items"]:
            assert item["direct_micros"] + item["carry_micros"] == item["total_micros"]

    def test_the_lanes_match_the_components_they_come_from(self, payload: dict) -> None:
        """Direct cost is the loading lane and carry is the cached lane, by construction."""
        for item in payload["items"]:
            assert item["lanes"]["loading_micros"] == item["direct_micros"]
            assert item["lanes"]["cached_micros"] == item["carry_micros"]

    def test_reads_and_residency_are_reported_per_item(self, payload: dict) -> None:
        read_file = next(i for i in payload["items"] if i["identity"].endswith("app.py"))
        assert read_file["reads"] >= 1
        assert read_file["turns_resident"] >= 1

    def test_per_session_figures_sum_to_the_item_total(self, payload: dict) -> None:
        for item in payload["items"]:
            assert sum(s["total_micros"] for s in item["per_session"]) == item["total_micros"]

    def test_an_item_below_the_models_minimum_is_flagged_not_buried(self, tmp_path: Path) -> None:
        """A ~10x per-turn difference on the same file across models (cost-model §2)."""
        builder = TranscriptBuilder()
        builder.add_turn(
            model="claude-opus-4-5",
            input_tokens=50,
            cache_creation_5m=9_000,
            output_tokens=20,
            tool_use_ids=("t1",),
        )
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/tiny.md", text="note\n" * 40)
        builder.add_turn(model="claude-opus-4-5", input_tokens=5, cache_read=9_400, output_tokens=9)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)

        tiny = next(i for i in payload["items"] if i["identity"].endswith("tiny.md"))
        assert tiny["never_cacheable_on"] == ["claude-opus-4-5"]
        assert any("full rate every turn" in note for note in payload["diagnostics"]["limitations"])


class TestScope:
    def test_the_exclusion_count_is_part_of_the_result(self, analysis: SessionAnalysis) -> None:
        payload = build_report_data([analysis], sessions_excluded_count=3, generated_at=FIXED_TIME)
        assert payload["scope"]["sessions_excluded_count"] == 3

    def test_an_in_progress_session_is_marked_provisional(self, tmp_path: Path) -> None:
        analysis = analyse(busy_session(), tmp_path, provisional=True)
        payload = build_report_data([analysis], generated_at=FIXED_TIME)
        assert payload["scope"]["provisional"] is True

    def test_sessions_analysed_under_different_policies_are_refused(self, tmp_path: Path) -> None:
        """Figures from two policies do not describe one breakdown, so they are not summed."""
        a = analyse(busy_session(), tmp_path, name="a", policy="proportional")
        b = analyse(busy_session(), tmp_path, name="b", policy="exclusive")
        with pytest.raises(ValueError, match="policies"):
            build_report_data([a, b], generated_at=FIXED_TIME)

    def test_an_empty_selection_is_the_callers_to_report(self) -> None:
        with pytest.raises(ValueError, match="zero analyses"):
            build_report_data([])


class TestRedaction:
    def test_it_removes_paths_but_keeps_the_costs_and_shares(
        self, analysis: SessionAnalysis
    ) -> None:
        clear = build_report_data([analysis], generated_at=FIXED_TIME)
        redacted = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)

        assert redacted["redacted"] is True
        assert redacted["totals"] == clear["totals"]
        assert [i["total_micros"] for i in redacted["items"]] == [
            i["total_micros"] for i in clear["items"]
        ]
        assert [i["share"] for i in redacted["items"]] == [i["share"] for i in clear["items"]]
        assert [i["category"] for i in redacted["items"]] == [i["category"] for i in clear["items"]]

    def test_no_path_survives_redaction(self, analysis: SessionAnalysis) -> None:
        redacted = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)
        for item in redacted["items"]:
            assert "identity" not in item
            assert "/repo" not in item["display"]
            assert "app.py" not in item["display"]

    def test_the_extension_survives_because_the_argument_is_about_extensions(
        self, analysis: SessionAnalysis
    ) -> None:
        redacted = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)
        assert any(item["display"].endswith(".md") for item in redacted["items"])

    def test_the_pseudonym_is_stable_across_runs(self, analysis: SessionAnalysis) -> None:
        first = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)
        second = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)
        assert [i["display"] for i in first["items"]] == [i["display"] for i in second["items"]]


class TestDeterminism:
    def test_two_runs_produce_byte_identical_payloads(self, analysis: SessionAnalysis) -> None:
        first = json.dumps(build_report_data([analysis], generated_at=FIXED_TIME), sort_keys=False)
        second = json.dumps(build_report_data([analysis], generated_at=FIXED_TIME), sort_keys=False)
        assert first == second

    def test_only_the_clock_reading_differs_without_a_pinned_timestamp(
        self, analysis: SessionAnalysis
    ) -> None:
        first = build_report_data([analysis])
        second = build_report_data([analysis])
        first.pop("generated_at")
        second.pop("generated_at")
        assert json.dumps(first) == json.dumps(second)

    def test_a_reanalysis_of_the_same_transcript_produces_the_same_payload(
        self, tmp_path: Path
    ) -> None:
        path = busy_session().write(tmp_path / "s.jsonl")
        one = analyse_transcript(path, pricing=PRICING)
        two = analyse_transcript(path, pricing=PRICING)
        assert json.dumps(build_report_data([one], generated_at=FIXED_TIME)) == json.dumps(
            build_report_data([two], generated_at=FIXED_TIME)
        )

    def test_the_payload_is_json_serialisable(self, payload: dict) -> None:
        assert json.loads(json.dumps(payload))["schema_version"] == SCHEMA_VERSION


class TestDeferredSections:
    def test_unbuilt_sections_are_empty_rather_than_invented(self, payload: dict) -> None:
        assert payload["tree"] == {}
        assert payload["turns"] == []
        assert payload["residency"] == []
        assert payload["invalidations"] == []
        assert payload["comparison"] == {}


def _occurrences(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = text.find(needle)
    while start != -1:
        positions.append(start)
        start = text.find(needle, start + 1)
    return positions
