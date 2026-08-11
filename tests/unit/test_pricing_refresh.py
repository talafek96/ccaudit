"""Contract on the pricing refresh.

The refresh exists so rates are not pinned to the tool's version. These tests fence the two
properties that make it safe: it never invents a cacheability threshold, and it never loses
information that was already hand-verified.

Every test here runs against an in-memory or on-disk source. Nothing in this file makes a
network request, and nothing in the analysis path ever does.
"""

import json
from pathlib import Path

import pytest

from ccaudit.config import BUNDLED_PRICING_PATH, MissingThresholdError, load_pricing
from ccaudit.config.refresh import (
    RefreshError,
    RefreshReport,
    build_table,
    read_source_file,
    refresh,
)

BUNDLED = load_pricing(BUNDLED_PRICING_PATH)


def source_payload(**overrides: dict[str, object]) -> dict[str, object]:
    """A LiteLLM-shaped rate table, trimmed to what the refresh actually reads."""
    payload: dict[str, object] = {
        "claude-opus-5": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 5e-06,
            "output_cost_per_token": 2.5e-05,
            "cache_creation_input_token_cost": 6.25e-06,
            "cache_read_input_token_cost": 5e-07,
        },
        "claude-haiku-4-5": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 1e-06,
            "output_cost_per_token": 5e-06,
        },
        "gpt-5": {
            "litellm_provider": "openai",
            "input_cost_per_token": 1e-06,
            "output_cost_per_token": 5e-06,
        },
    }
    payload.update(overrides)
    return payload


def set_rate(payload: dict[str, object], model: str, key: str, value: float) -> None:
    """Adjust one rate in a source payload without reaching through an untyped index."""
    entry = payload[model]
    assert isinstance(entry, dict), f"{model} entry is not an object"
    entry[key] = value


def build(payload: dict[str, object], today: str = "2026-09-01") -> tuple[str, RefreshReport]:
    return build_table(payload, BUNDLED, source_name="test-source", today=today)


class TestBuildTable:
    def test_unchanged_rates_are_reported_as_unchanged(self) -> None:
        _, report = build(source_payload())
        assert "claude-opus-5" in report.unchanged
        assert not report.updated

    def test_a_price_change_is_applied_and_named(self) -> None:
        payload = source_payload()
        set_rate(payload, "claude-opus-5", "input_cost_per_token", 6e-06)
        text, report = build(payload)
        assert any("claude-opus-5" in line for line in report.updated)
        assert "input_usd_per_mtok = 6.0" in text

    def test_a_new_model_is_added_but_flagged_as_needing_a_threshold(self) -> None:
        """Rate sources do not publish min_cacheable_tokens. We refuse to invent one."""
        payload = source_payload(
            **{
                "claude-opus-6": {
                    "litellm_provider": "anthropic",
                    "input_cost_per_token": 4e-06,
                    "output_cost_per_token": 2e-05,
                }
            }
        )
        text, report = build(payload)
        assert "claude-opus-6" in report.added
        assert "claude-opus-6" in report.needs_threshold
        assert "# min_cacheable_tokens = ?" in text

    def test_a_new_model_without_a_threshold_raises_on_use(self, tmp_path: Path) -> None:
        payload = source_payload(
            **{
                "claude-opus-6": {
                    "litellm_provider": "anthropic",
                    "input_cost_per_token": 4e-06,
                    "output_cost_per_token": 2e-05,
                }
            }
        )
        text, _ = build(payload)
        table = tmp_path / "pricing.toml"
        table.write_text(text, encoding="utf-8")
        pricing = load_pricing(table)
        assert pricing.for_model("claude-opus-6").input_micros_per_mtok == 4_000_000
        with pytest.raises(MissingThresholdError):
            pricing.min_cacheable_tokens("claude-opus-6")

    def test_existing_thresholds_survive_a_refresh(self, tmp_path: Path) -> None:
        """A refresh may only add information about rates — never lose a verified threshold."""
        text, _ = build(source_payload())
        table = tmp_path / "pricing.toml"
        table.write_text(text, encoding="utf-8")
        refreshed = load_pricing(table)
        for model_id, model in BUNDLED.models.items():
            assert refreshed.min_cacheable_tokens(model_id) == model.min_cacheable_tokens

    def test_models_absent_from_the_source_are_kept_and_reported(self) -> None:
        _, report = build(source_payload())
        assert "claude-sonnet-5" in report.missing_from_source

    def test_non_anthropic_models_are_ignored(self) -> None:
        text, report = build(source_payload())
        assert "gpt-5" not in text
        assert "gpt-5" not in report.added

    def test_an_empty_source_refuses_to_overwrite_a_working_table(self) -> None:
        with pytest.raises(RefreshError, match="refusing to overwrite"):
            build({})

    def test_a_source_with_only_other_providers_is_empty_too(self) -> None:
        with pytest.raises(RefreshError, match="refusing to overwrite"):
            build({"gpt-5": {"litellm_provider": "openai", "input_cost_per_token": 1e-06}})

    def test_multiplier_divergence_is_reported_not_applied(self) -> None:
        """The multipliers are a documented API property. A source that disagrees is a
        finding for a human, not a number to silently overwrite the table with."""
        payload = source_payload()
        set_rate(payload, "claude-opus-5", "cache_read_input_token_cost", 1e-06)
        text, report = build(payload)
        assert any("cache read multiplier" in note for note in report.multiplier_notes)
        assert "read_multiplier = 0.1" in text

    def test_the_written_table_records_where_the_rates_came_from(self) -> None:
        text, _ = build(source_payload(), today="2026-09-01")
        assert "test-source" in text
        assert 'priced_on = "2026-09-01"' in text

    def test_the_written_table_round_trips(self, tmp_path: Path) -> None:
        text, _ = build(source_payload())
        table = tmp_path / "pricing.toml"
        table.write_text(text, encoding="utf-8")
        refreshed = load_pricing(table)
        assert refreshed.models.keys() >= BUNDLED.models.keys()
        assert float(refreshed.cache.write_1h) == 2.0


class TestRefreshCommand:
    def test_writes_the_user_table_from_a_local_source(self, tmp_path: Path) -> None:
        source = tmp_path / "rates.json"
        source.write_text(json.dumps(source_payload()), encoding="utf-8")
        destination = tmp_path / "state" / "pricing.toml"

        report = refresh(destination=destination, source_file=source, today="2026-09-01")

        assert destination.is_file()
        assert report.fetched_on == "2026-09-01"
        assert load_pricing(destination).priced_on == "2026-09-01"

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        source = tmp_path / "rates.json"
        source.write_text(json.dumps(source_payload()), encoding="utf-8")
        destination = tmp_path / "pricing.toml"

        report = refresh(destination=destination, source_file=source, dry_run=True)

        assert not destination.exists()
        assert report.lines()

    def test_a_second_refresh_builds_on_the_first(self, tmp_path: Path) -> None:
        source = tmp_path / "rates.json"
        source.write_text(json.dumps(source_payload()), encoding="utf-8")
        destination = tmp_path / "pricing.toml"
        refresh(destination=destination, source_file=source, today="2026-09-01")

        payload = source_payload()
        set_rate(payload, "claude-opus-5", "input_cost_per_token", 7e-06)
        source.write_text(json.dumps(payload), encoding="utf-8")
        refresh(destination=destination, source_file=source, today="2026-09-02")

        pricing = load_pricing(destination)
        assert pricing.for_model("claude-opus-5").input_micros_per_mtok == 7_000_000
        assert pricing.min_cacheable_tokens("claude-opus-5") == 512

    def test_a_missing_source_file_raises_and_leaves_the_table_alone(self, tmp_path: Path) -> None:
        destination = tmp_path / "pricing.toml"
        destination.write_text(BUNDLED_PRICING_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        before = destination.read_text(encoding="utf-8")

        with pytest.raises(RefreshError, match="not found"):
            refresh(destination=destination, source_file=tmp_path / "absent.json")

        assert destination.read_text(encoding="utf-8") == before

    def test_malformed_json_is_rejected_by_name(self, tmp_path: Path) -> None:
        source = tmp_path / "rates.json"
        source.write_text("{not json", encoding="utf-8")
        with pytest.raises(RefreshError, match="not valid JSON"):
            read_source_file(source)

    def test_report_lines_name_what_changed(self, tmp_path: Path) -> None:
        source = tmp_path / "rates.json"
        payload = source_payload()
        set_rate(payload, "claude-opus-5", "input_cost_per_token", 6e-06)
        source.write_text(json.dumps(payload), encoding="utf-8")

        report = refresh(destination=tmp_path / "p.toml", source_file=source, dry_run=True)

        joined = "\n".join(report.lines())
        assert "rate changed" in joined
        assert "claude-opus-5" in joined
