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
from ccaudit.render.data import SCHEMA_VERSION, build_report_data, collapse_notes
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

    def test_an_estimated_size_lowers_the_precision_and_names_the_driver(
        self, tmp_path: Path
    ) -> None:
        """CHANGED from "drops to one figure": one figure moved the number by more than the
        uncertainty it stood for. What must hold is that the precision is the *reduced* one and
        that the item says what drives its range."""
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=50, cache_creation_5m=9_000, output_tokens=20)
        builder.add_tool_schema_delta(added=["mcp__demo__go"], added_lines=400)
        builder.add_turn(input_tokens=5, cache_read=9_400, output_tokens=15)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)

        estimated = [item for item in payload["items"] if item["basis"] == "estimated"]
        assert estimated
        assert all(item["display_sig_figs"] == sig_figs_for("low") for item in estimated)
        assert all(item["display_sig_figs"] < sig_figs_for("high") for item in estimated)
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

    def test_the_lanes_partition_the_item_figures(self, payload: dict) -> None:
        """Direct cost is the write lane; carry spans the cache-rate and the full-rate lanes.

        Contract change, deliberate and human-approved: the earlier form asserted
        ``cached_micros == carry_micros``, which was true only while the sub-threshold lane was
        reported as zero. Content below the model's minimum cacheable prefix is charged as
        carry at *full* rate (cost-model §5.2), so folding it into the cached lane would state
        a tenth of what it cost. The invariant that survives is that the lanes partition the
        item's figures exactly.
        """
        for item in payload["items"]:
            lanes = item["lanes"]
            assert lanes["loading_micros"] == item["direct_micros"]
            assert lanes["cached_micros"] + lanes["uncached_micros"] == item["carry_micros"]
            assert min(lanes.values()) >= 0

    def test_only_a_sub_threshold_item_is_charged_in_the_full_rate_lane(
        self, payload: dict
    ) -> None:
        """``uncached_micros > 0`` is a claim about the model's minimum, so it must be one."""
        for item in payload["items"]:
            if item["lanes"]["uncached_micros"]:
                assert item["never_cacheable_on"]

    def test_a_model_is_named_only_where_the_item_was_actually_resident_on_it(
        self, tmp_path: Path
    ) -> None:
        """FR-078 is a finding about observed turns, not a size-against-every-model sweep.

        The small file arrives only after the session has moved to a second model, so it was
        never in context alongside the first one. Comparing every item's size against every
        model the session used would name that first model anyway — a flag on a pairing that
        never happened, in the direction that reads as a finding.
        """
        builder = TranscriptBuilder()
        builder.add_turn(
            model="claude-opus-4-5",
            input_tokens=50,
            cache_creation_5m=9_000,
            output_tokens=20,
            tool_use_ids=("t1",),
        )
        builder.add_tool_result(tool_use_id="t1", file_path="/repo/app.py", text="x = 1\n" * 200)
        builder.add_turn(
            model="claude-opus-4-5",
            input_tokens=400,
            cache_read=9_400,
            output_tokens=9,
            tool_use_ids=("t2",),
        )
        builder.add_tool_result(tool_use_id="t2", file_path="/repo/tiny.md", text="note\n" * 40)
        builder.add_turn(model="claude-opus-5", input_tokens=400, cache_read=9_400, output_tokens=9)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)

        tiny = next(i for i in payload["items"] if i["identity"].endswith("tiny.md"))
        assert tiny["never_cacheable_on"] == ["claude-opus-5"]

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


class TestTree:
    """FR-034 — the hierarchy carries both measures, and it partitions at every level."""

    def test_the_root_is_the_whole_session(self, payload: dict) -> None:
        assert payload["tree"]["total_micros"] == payload["totals"]["cost_micros"]
        assert payload["tree"]["share"] == 1.0

    def test_every_node_is_its_children_plus_its_own_cost(self, payload: dict) -> None:
        def check(node: dict) -> None:
            covered = node["flat_micros"] + sum(c["total_micros"] for c in node["children"])
            assert covered == node["total_micros"], node["path"]
            for child in node["children"]:
                check(child)

        check(payload["tree"])

    def test_the_remainder_is_a_node_of_its_own(self, payload: dict) -> None:
        """A part-to-whole view must show what it could not attribute (FR-040)."""
        remainder = next(
            node for node in payload["tree"]["children"] if node["path"] == "unattributed"
        )
        assert remainder["flat_micros"] == payload["totals"]["unattributed_micros"]

    def test_the_conclusions_that_belong_to_no_file_are_named_at_the_root(
        self, payload: dict
    ) -> None:
        by_id = {c["id"]: c["cost_micros"] for c in payload["attribution"]}
        by_path = {node["path"]: node["flat_micros"] for node in payload["tree"]["children"]}
        assert by_path["(output)"] == by_id["output"]
        assert by_path["(overhead)"] == by_id["overhead"]

    def test_an_item_with_no_folder_gets_a_named_bucket_not_an_invented_one(
        self, tmp_path: Path
    ) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=50, cache_creation_5m=9_000, output_tokens=20)
        builder.add_skill_listing(names=["demo"], content="skill: demo\n" * 60)
        builder.add_turn(input_tokens=5, cache_read=9_400, output_tokens=15)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)

        paths = {node["path"] for node in payload["tree"]["children"]}
        assert "/(skill)" in paths

    def test_a_node_never_claims_more_precision_than_its_contents(self, payload: dict) -> None:
        def check(node: dict) -> None:
            for child in node["children"]:
                assert child["display_sig_figs"] >= node["display_sig_figs"]
                check(child)

        check(payload["tree"])

    def test_redaction_keeps_the_shape_and_removes_the_path(
        self, analysis: SessionAnalysis
    ) -> None:
        clear = build_report_data([analysis], generated_at=FIXED_TIME)["tree"]
        redacted = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)["tree"]

        def shape(node: dict) -> list:
            return [node["total_micros"], [shape(child) for child in node["children"]]]

        def paths(node: dict) -> list[str]:
            return [node["path"], *(p for child in node["children"] for p in paths(child))]

        assert shape(redacted) == shape(clear)
        assert not any("repo" in path for path in paths(redacted))


class TestTurns:
    """FR-039, FR-083 — per-turn accumulation, and the prompt size stated honestly."""

    def test_the_turns_sum_to_the_session_total(self, payload: dict) -> None:
        assert sum(t["cost_micros"] for t in payload["turns"]) == payload["totals"]["cost_micros"]

    def test_ordinals_run_from_one_without_a_gap(self, payload: dict) -> None:
        assert [t["ordinal"] for t in payload["turns"]] == list(range(1, len(payload["turns"]) + 1))

    def test_each_turns_components_sum_to_its_cost(self, payload: dict) -> None:
        for turn in payload["turns"]:
            assert sum(turn["components"].values()) == turn["cost_micros"]

    def test_prompt_tokens_is_the_whole_conversation_not_the_uncached_remainder(
        self, payload: dict, analysis: SessionAnalysis
    ) -> None:
        """A session showing 4K fresh input after hours of work is not a small session."""
        for turn, record in zip(payload["turns"], analysis.timeline.turns, strict=True):
            usage = record.usage
            assert turn["prompt_tokens"] == (
                usage.input_tokens + usage.cache_creation_tokens + usage.cache_read_tokens
            )
        assert any(t["prompt_tokens"] > t["components"]["fresh_input"] for t in payload["turns"])

    def test_a_compaction_boundary_is_marked_with_what_it_dropped(self, tmp_path: Path) -> None:
        builder = busy_session()
        builder.add_compaction(pre_tokens=15_500, post_tokens=4_000, preserved_uuids=[])
        builder.add_turn(input_tokens=8, cache_creation_5m=4_000, output_tokens=30)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)

        compacted = [t for t in payload["turns"] if t["compaction"]]
        assert len(compacted) == 1
        assert compacted[0]["compaction"] == {
            "occurred": True,
            "pre_tokens": 15_500,
            "post_tokens": 4_000,
            "dropped_tokens": 11_500,
        }

    def test_a_turn_without_a_boundary_says_so_rather_than_omitting_the_key(
        self, payload: dict
    ) -> None:
        assert all(turn["compaction"] is None for turn in payload["turns"])


class TestResidency:
    """FR-036 — how long each item stayed, and what it was charged at while it did."""

    def test_there_is_one_row_per_span(self, payload: dict, analysis: SessionAnalysis) -> None:
        assert len(payload["residency"]) == len(analysis.timeline.spans)

    def test_a_span_still_resident_at_the_end_says_so_with_null(self, payload: dict) -> None:
        open_spans = [row for row in payload["residency"] if row["last_turn"] is None]
        assert open_spans
        assert all(row["end_reason"] == "session_end" for row in open_spans)

    def test_turns_are_one_based_and_inside_the_session(self, payload: dict) -> None:
        last = len(payload["turns"])
        for row in payload["residency"]:
            assert 1 <= row["first_turn"] <= last
            assert row["last_turn"] is None or row["first_turn"] <= row["last_turn"] <= last

    def test_the_lane_breakdown_covers_every_turn_of_the_span_or_none_of_it(
        self, payload: dict
    ) -> None:
        """A filler lane would be a price claim: the three lanes differ by 10x."""
        last = len(payload["turns"])
        for row in payload["residency"]:
            if not row["lane_by_turn"]:
                continue
            end = last if row["last_turn"] is None else row["last_turn"]
            assert len(row["lane_by_turn"]) == end - row["first_turn"] + 1
            assert set(row["lane_by_turn"]) <= {"cached", "uncached", "loading"}

    def test_it_names_items_exactly_as_the_item_rows_do(self, analysis: SessionAnalysis) -> None:
        for redact in (False, True):
            payload = build_report_data([analysis], redact=redact, generated_at=FIXED_TIME)
            named = {item["item_id"] for item in payload["items"]}
            assert {row["item_id"] for row in payload["residency"]} <= named

    def test_no_path_survives_redaction(self, analysis: SessionAnalysis) -> None:
        redacted = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)
        for row in redacted["residency"]:
            assert "/repo" not in row["display"]
            assert "/repo" not in row["item_id"]


class TestComparison:
    """FR-037 — the question the tool was commissioned to settle, on one scale."""

    def test_the_two_sides_account_for_every_item(self, payload: dict) -> None:
        comparison = payload["comparison"]
        covered = sum(
            entry["cost_micros"]
            for key, series in comparison.items()
            if key != "note"
            for entry in series
        )
        assert covered == sum(item["total_micros"] for item in payload["items"])

    def test_an_instruction_file_is_not_counted_as_a_work_driven_read(self, payload: dict) -> None:
        """The disputed claim is about instruction content, not about every `.md`."""
        instruction = payload["comparison"]["resident_instruction"]
        assert any(entry["label"] == "Instruction files" for entry in instruction)
        assert all(
            entry["label"] != "Instruction files"
            for entry in payload["comparison"]["work_driven_reads"]
        )

    def test_read_documentation_stays_on_the_reads_side(self, payload: dict) -> None:
        assert any(entry["label"] == "docs" for entry in payload["comparison"]["work_driven_reads"])

    def test_every_entry_carries_both_measures_and_a_share(self, payload: dict) -> None:
        for key in ("resident_instruction", "work_driven_reads"):
            for entry in payload["comparison"][key]:
                assert entry["tokens"] > 0
                assert entry["cost_micros"] >= 0
                assert 0.0 <= entry["share"] <= 1.0

    def test_entries_are_ranked_most_expensive_first(self, payload: dict) -> None:
        for key in ("resident_instruction", "work_driven_reads"):
            costs = [entry["cost_micros"] for entry in payload["comparison"][key]]
            assert costs == sorted(costs, reverse=True)

    def test_the_note_says_what_makes_the_two_series_different(self, payload: dict) -> None:
        note = payload["comparison"]["note"].lower()
        assert "every turn" in note
        assert "read" in note

    def test_it_is_empty_rather_than_invented_when_nothing_was_attributed(
        self, tmp_path: Path
    ) -> None:
        builder = TranscriptBuilder()
        builder.add_ui_noise(6)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)
        assert payload["comparison"] == {}


def edited_instruction_session() -> TranscriptBuilder:
    """A session where an instruction file is edited mid-flight, forcing a re-write."""
    builder = TranscriptBuilder()
    builder.add_turn(input_tokens=50, cache_creation_5m=9_000, output_tokens=20)
    builder.add_attachment(
        "edited_text_file",
        {
            "displayPath": "/repo/docs/CLAUDE.md",
            "filename": "CLAUDE.md",
            "content": {"file": {"filePath": "/repo/docs/CLAUDE.md", "content": "# Rules\n" * 80}},
        },
    )
    builder.add_turn(input_tokens=9, cache_creation_5m=9_000, cache_read=400, output_tokens=25)
    return builder


class TestInvalidations:
    def test_a_forced_reload_names_the_change_that_caused_it(self, tmp_path: Path) -> None:
        payload = build_report_data(
            [analyse(edited_instruction_session(), tmp_path)], generated_at=FIXED_TIME
        )
        assert payload["invalidations"]
        assert any("CLAUDE.md" in event["detail"] for event in payload["invalidations"])

    def test_a_forced_reload_detail_does_not_leak_a_path_under_redaction(
        self, tmp_path: Path
    ) -> None:
        """The sentence names the file that changed, so it is pseudonymised like any other."""
        analysis = analyse(edited_instruction_session(), tmp_path)
        redacted = build_report_data([analysis], redact=True, generated_at=FIXED_TIME)
        assert redacted["invalidations"]
        for event in redacted["invalidations"]:
            assert "/repo" not in event["detail"]
            # The shape of the finding survives: it still says an instruction file changed.
            assert "instruction file" in event["detail"]


def _occurrences(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = text.find(needle)
    while start != -1:
        positions.append(start)
        start = text.find(needle, start + 1)
    return positions


class TestNotesStayReadableAtCorpusScale:
    """Thirty copies of one limitation is a page that says one thing.

    Analysing a whole machine produced ~30 "this session spans Claude Code versions ..." notes
    and 8 compaction notes, each differing only in its figures, ahead of anything actionable.
    They collapse to one line that carries the limitation verbatim and says how widespread it
    is. Nothing is summed: a combined figure invented from prose would be a made-up number.
    """

    def test_identical_notes_collapse(self) -> None:
        assert collapse_notes(["same note.", "same note."]) == ["same note."]

    def test_notes_differing_only_in_figures_collapse_and_are_counted(self) -> None:
        collapsed = collapse_notes(
            [
                "This session spans Claude Code versions 2.1.202, 2.1.205; take care.",
                "This session spans Claude Code versions 2.1.212, 2.1.220; take care.",
                "This session spans Claude Code versions 2.1.1, 2.1.2, 2.1.3; take care.",
            ]
        )
        assert len(collapsed) == 1
        assert collapsed[0].startswith("This session spans Claude Code versions 2.1.202, 2.1.205")
        assert "and 2 more session(s)" in collapsed[0]

    def test_the_first_instance_survives_verbatim(self) -> None:
        """The reader still gets a real, checkable example rather than a template."""
        collapsed = collapse_notes(["About 466,486 tokens left.", "About 4,604,222 tokens left."])
        assert collapsed[0].startswith("About 466,486 tokens left.")

    def test_genuinely_different_notes_are_kept_apart(self) -> None:
        notes = ["Rates were published 2026-08-11.", "About 5 tokens left."]
        assert len(collapse_notes(notes)) == 2

    def test_no_figure_is_invented_by_the_collapse(self) -> None:
        """The only number added is a count of sessions, never a sum of their figures."""
        collapsed = collapse_notes(["About 100 tokens left.", "About 200 tokens left."])
        assert "300" not in collapsed[0]

    def test_it_is_order_preserving_and_deterministic(self) -> None:
        notes = ["b note 1.", "a note 2.", "b note 3."]
        assert collapse_notes(notes) == collapse_notes(notes)
        assert collapse_notes(notes)[0].startswith("b note 1.")
