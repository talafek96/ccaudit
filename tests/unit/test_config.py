"""Contract on the configuration registry (Principle IX) and the honesty rules it enforces."""

from pathlib import Path

import pytest

from ccaudit.config import (
    ATTRIBUTION_COMPONENTS,
    BUNDLED_PRICING_PATH,
    CHARGE_COMPONENTS,
    MissingThresholdError,
    UnknownModelError,
    attribution_component,
    ccaudit_home,
    charge_component,
    clear_pricing_cache,
    load_pricing,
    resolve_pricing_path,
    sig_figs_for,
)
from ccaudit.config.categories import CATEGORIES, categorize
from ccaudit.money import cost_micros

BUNDLED = load_pricing(BUNDLED_PRICING_PATH)


class TestComponentRegistry:
    def test_four_charge_components(self) -> None:
        assert {c.id for c in CHARGE_COMPONENTS} == {
            "fresh_input",
            "cache_write",
            "cache_read",
            "output",
        }

    def test_four_attribution_components(self) -> None:
        assert {c.id for c in ATTRIBUTION_COMPONENTS} == {
            "direct",
            "carry",
            "overhead",
            "output",
        }

    def test_every_component_has_a_plain_language_name(self) -> None:
        """Jargon only the author understands is a defect (Principle X, FR-016)."""
        for component in (*CHARGE_COMPONENTS, *ATTRIBUTION_COMPONENTS):
            assert component.plain_name
            assert component.plain_name != component.technical_name
            assert component.description.endswith(".")

    def test_plain_names_are_the_mandated_ones(self) -> None:
        by_id = {c.id: c.plain_name for c in CHARGE_COMPONENTS}
        assert by_id["cache_write"] == "Loading into context"
        assert by_id["cache_read"] == "Keeping context loaded"
        assert by_id["fresh_input"] == "Your new typing"
        assert by_id["output"] == "What Claude wrote back"

    def test_unknown_component_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(KeyError, match="unknown charge component"):
            charge_component("cache_creation")
        with pytest.raises(KeyError, match="unknown attribution component"):
            attribution_component("indirect")

    def test_confidence_drives_displayed_precision(self) -> None:
        assert sig_figs_for("high") > sig_figs_for("medium") > sig_figs_for("low")

    def test_unknown_confidence_raises(self) -> None:
        with pytest.raises(KeyError, match="unknown confidence"):
            sig_figs_for("probably-fine")


class TestPricingTable:
    def test_bundled_table_loads(self) -> None:
        assert BUNDLED.schema_version == 1
        assert BUNDLED.models

    def test_unknown_model_raises_rather_than_defaulting(self) -> None:
        """A neighbouring model's rate would be a plausible, confidently wrong figure."""
        with pytest.raises(UnknownModelError, match="no pricing for model"):
            BUNDLED.for_model("claude-opus-9")

    def test_the_error_names_what_to_do(self) -> None:
        with pytest.raises(UnknownModelError) as exc:
            BUNDLED.for_model("gpt-5")
        assert "pricing.toml" in str(exc.value)
        assert "not monotonic" in str(exc.value)

    def test_dated_snapshot_ids_resolve(self) -> None:
        """Transcripts record decorated ids; they name the same priced model."""
        assert BUNDLED.for_model("claude-haiku-4-5-20251001").model_id == "claude-haiku-4-5"

    def test_context_window_marker_resolves(self) -> None:
        assert BUNDLED.for_model("claude-opus-5[1m]").model_id == "claude-opus-5"

    def test_vendor_prefix_resolves(self) -> None:
        assert BUNDLED.for_model("anthropic.claude-opus-5").model_id == "claude-opus-5"


class TestCacheabilityThreshold:
    """The trap this table exists to prevent, pinned as a contract.

    The minimum cacheable prefix does NOT decrease monotonically across model generations, so
    it can never be inferred from a version ordering. A model that assumed otherwise would
    misprice exactly the small instruction files this tool exists to price.
    """

    def test_thresholds_are_not_monotonic_across_generations(self) -> None:
        opus_47 = BUNDLED.min_cacheable_tokens("claude-opus-4-7")
        opus_5 = BUNDLED.min_cacheable_tokens("claude-opus-5")
        assert opus_47 == 2048
        assert opus_5 == 512
        assert opus_5 < opus_47, "a newer model requires a SMALLER prefix — never derive this"

    def test_the_984_token_claude_md_case(self) -> None:
        """The measured case from docs/cost-model.md: same file, 10x different per-turn cost."""
        claude_md_tokens = 984
        assert claude_md_tokens >= BUNDLED.min_cacheable_tokens("claude-opus-5")
        assert claude_md_tokens < BUNDLED.min_cacheable_tokens("claude-opus-4-6")

    def test_every_bundled_model_has_a_threshold(self) -> None:
        for model_id in BUNDLED.models:
            assert BUNDLED.min_cacheable_tokens(model_id) > 0

    def test_a_missing_threshold_raises_rather_than_defaulting(self, tmp_path: Path) -> None:
        """A refreshed table can gain a model with no published threshold. Never guess one."""
        table = tmp_path / "pricing.toml"
        table.write_text(
            "schema_version = 1\n"
            "[cache]\n"
            "read_multiplier = 0.1\n"
            "write_multiplier_5m = 1.25\n"
            "write_multiplier_1h = 2.0\n"
            "unknown_ttl_multiplier = 1.25\n"
            'unknown_ttl_confidence = "low"\n'
            '[models."claude-future-9"]\n'
            "input_usd_per_mtok = 5.0\n"
            "output_usd_per_mtok = 25.0\n",
            encoding="utf-8",
        )
        pricing = load_pricing(table)
        assert pricing.for_model("claude-future-9").input_micros_per_mtok == 5_000_000
        with pytest.raises(MissingThresholdError, match="do not publish it"):
            pricing.min_cacheable_tokens("claude-future-9")


class TestCacheMultipliers:
    def test_write_multiplier_doubles_with_the_ttl(self) -> None:
        five_min, _ = BUNDLED.cache.write_multiplier("5m")
        one_hour, _ = BUNDLED.cache.write_multiplier("1h")
        assert one_hour == 2 * five_min * 4 / 5  # 2.0 vs 1.25
        assert float(one_hour) == 2.0
        assert float(five_min) == 1.25

    def test_read_is_a_tenth(self) -> None:
        assert float(BUNDLED.cache.read) == 0.1

    def test_unknown_ttl_downgrades_confidence_rather_than_assuming(self) -> None:
        """Silently assuming 5m would understate reload cost by up to 60% and never say so."""
        multiplier, confidence_cap = BUNDLED.cache.write_multiplier(None)
        assert float(multiplier) == 1.25
        assert confidence_cap == "low"

    def test_a_known_ttl_imposes_no_confidence_cap(self) -> None:
        _, confidence_cap = BUNDLED.cache.write_multiplier("1h")
        assert confidence_cap is None

    def test_multipliers_price_a_turn_end_to_end(self) -> None:
        model = BUNDLED.for_model("claude-opus-5")
        write, _ = BUNDLED.cache.write_multiplier("5m")
        assert cost_micros(1_000_000, model.input_micros_per_mtok, write) == 6_250_000
        assert cost_micros(1_000_000, model.input_micros_per_mtok, BUNDLED.cache.read) == 500_000


class TestTableResolution:
    """Rates must not be pinned to the tool's version — see config/refresh.py."""

    def test_explicit_env_path_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCAUDIT_PRICING", str(tmp_path / "team.toml"))
        path, origin = resolve_pricing_path()
        assert path == tmp_path / "team.toml"
        assert "explicit" in origin

    def test_user_table_beats_the_bundled_seed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CCAUDIT_PRICING", raising=False)
        monkeypatch.setenv("CCAUDIT_HOME", str(tmp_path))
        (tmp_path / "pricing.toml").write_text("schema_version = 1\n", encoding="utf-8")
        path, origin = resolve_pricing_path()
        assert path == tmp_path / "pricing.toml"
        assert origin == "refreshed"

    def test_falls_back_to_the_bundled_seed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CCAUDIT_PRICING", raising=False)
        monkeypatch.setenv("CCAUDIT_HOME", str(tmp_path))
        path, origin = resolve_pricing_path()
        assert path == BUNDLED_PRICING_PATH
        assert origin == "bundled"

    def test_home_is_created_on_demand_not_at_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CCAUDIT_HOME", str(tmp_path / "state"))
        assert ccaudit_home() == tmp_path / "state"

    def test_provenance_names_the_table_and_its_date(self) -> None:
        """How old the rates are is part of a figure's basis, not a footnote."""
        assert "pricing table" in BUNDLED.provenance
        assert BUNDLED.priced_on in BUNDLED.provenance

    def test_a_missing_table_raises_with_the_path(self, tmp_path: Path) -> None:
        clear_pricing_cache()
        with pytest.raises(FileNotFoundError, match="CCAUDIT_PRICING"):
            load_pricing(tmp_path / "absent.toml")


class TestCategories:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("CLAUDE.md", "docs"),
            ("docs/cost-model.md", "docs"),
            ("README.rst", "docs"),
            ("src/ccaudit/money.py", "source"),
            ("app/main.go", "source"),
            ("specs/001-feature/spec.md", "spec"),
            (".specify/memory/constitution.md", "spec"),
            (".claude/skills/prior-art/SKILL.md", "skill"),
            ("uv.lock", "other"),
            ("data/corpus.json", "other"),
        ],
    )
    def test_paths_land_in_the_expected_category(self, path: str, expected: str) -> None:
        assert categorize(path).category == expected

    def test_kind_wins_over_the_path(self) -> None:
        assert categorize("Read", kind="tool_schema").category == "schema"
        assert categorize("playwright/navigate", kind="mcp_schema").category == "schema"

    def test_every_result_explains_itself(self) -> None:
        """--explain has to be able to show why a file landed where it did."""
        assert categorize("src/x.py").reason
        assert categorize("nothing.xyz").reason == "no category rule matched"

    def test_all_results_are_declared_categories(self) -> None:
        for path in ("a.py", "b.md", "specs/c.md", "d.bin", "skills/e/SKILL.md"):
            assert categorize(path).category in CATEGORIES
