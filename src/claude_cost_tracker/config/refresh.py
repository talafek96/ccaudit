"""Refresh the pricing table from a public rate source — explicit, opt-in, never automatic.

**Why this exists.** Rates, cache multipliers, and cacheability minimums drift. Pinning them
to the tool's version means every installed copy goes quietly stale and someone has to chase
a release to fix a number. So the table is a *user-owned file* that a refresh updates in
place, under ``CCOST_HOME``, surviving upgrades.

**What this does not do.** It is not on the analysis path. ``ccost`` analysing a session
makes no network request, needs no credential, and works fully offline (FR-029, FR-030,
SC-011); only ``ccost pricing refresh``, typed by the user, reaches out. Nothing about the
user's sessions is transmitted — the request is a plain GET for a public rate table, carrying
no session data of any kind.

**What it refuses to do.** Rate sources publish prices; they do not publish the *minimum
cacheable prefix*. A refresh therefore preserves existing thresholds and leaves new models
without one, so the loader raises rather than lane-classifying against an invented number.
Guessing a threshold misprices small instruction files by ~10x in the direction that hides
the error, which is the one mistake this tool cannot afford (docs/cost-model.md §2).
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from claude_cost_tracker.config import (
    BUNDLED_PRICING_PATH,
    Pricing,
    clear_pricing_cache,
    load_pricing,
    user_pricing_path,
)
from claude_cost_tracker.money import MICROS_PER_DOLLAR, TOKENS_PER_MTOK

# LiteLLM's table is the ecosystem's machine-readable rate source: it carries per-token input,
# output, cache-write and cache-read costs for every Anthropic model, and it is what the rest
# of the Claude Code cost tooling already tracks. Override it with --source-url for a
# team-managed mirror, or --from for an air-gapped copy.
DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
DEFAULT_TIMEOUT_SECONDS = 20
_USER_AGENT = "ccost-pricing-refresh"


class RefreshError(RuntimeError):
    """The refresh could not produce a usable table. The existing one is left untouched."""


@dataclass
class RefreshReport:
    """What a refresh changed, and what it deliberately did not.

    Printed in full: a rate change that moves every figure in every past report is not a
    silent success (Principle VI — traceable from output).
    """

    source: str
    fetched_on: str
    updated: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing_from_source: list[str] = field(default_factory=list)
    needs_threshold: list[str] = field(default_factory=list)
    multiplier_notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.updated or self.added)

    def lines(self) -> list[str]:
        """Human-readable summary lines, most consequential first."""
        out = [f"source: {self.source}", f"fetched: {self.fetched_on}"]
        if self.added:
            out.append(f"added {len(self.added)} model(s): {', '.join(sorted(self.added))}")
        if self.updated:
            out.append(f"rate changed for {len(self.updated)}: {', '.join(sorted(self.updated))}")
        if self.unchanged:
            out.append(f"unchanged: {len(self.unchanged)} model(s)")
        if self.missing_from_source:
            out.append(
                f"kept (absent from source, rates unverified): "
                f"{', '.join(sorted(self.missing_from_source))}"
            )
        if self.needs_threshold:
            out.append(
                f"NEEDS min_cacheable_tokens (fill in by hand; cannot be derived): "
                f"{', '.join(sorted(self.needs_threshold))}"
            )
        out.extend(self.multiplier_notes)
        if not self.changed:
            out.append("no rate changes")
        return out


def fetch_source(
    url: str = DEFAULT_SOURCE_URL, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, object]:
    """GET the rate table. The only network call in the tool, and only from this command."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.URLError as exc:
        raise RefreshError(f"could not fetch {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RefreshError(f"timed out fetching {url} after {timeout}s") from exc
    return _parse_source(payload, url)


def read_source_file(path: Path) -> dict[str, object]:
    """Read a rate table from disk, for an air-gapped or pinned refresh."""
    if not path.is_file():
        raise RefreshError(f"rate source not found: {path}")
    return _parse_source(path.read_bytes(), str(path))


def _parse_source(payload: bytes, origin: str) -> dict[str, object]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RefreshError(f"{origin} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RefreshError(f"{origin} did not contain a JSON object of models")
    return parsed


def build_table(
    source: dict[str, object],
    current: Pricing,
    *,
    source_name: str,
    today: str,
) -> tuple[str, RefreshReport]:
    """Merge fetched rates onto the current table and render the replacement TOML.

    Merge, not replace: thresholds and any model the source does not carry are preserved, so
    a refresh can only ever add information about rates — never lose what was hand-verified.
    """
    report = RefreshReport(source=source_name, fetched_on=today)
    rates = _anthropic_rates(source)
    if not rates:
        raise RefreshError(
            f"{source_name} contained no recognisable Anthropic model rates; refusing to "
            f"overwrite a working table with an empty one"
        )

    merged: dict[str, dict[str, object]] = {}
    for model_id, model in current.models.items():
        entry: dict[str, object] = {
            "input_usd_per_mtok": _micros_per_mtok_to_usd(model.input_micros_per_mtok),
            "output_usd_per_mtok": _micros_per_mtok_to_usd(model.output_micros_per_mtok),
        }
        if model.min_cacheable_tokens is not None:
            entry["min_cacheable_tokens"] = model.min_cacheable_tokens

        fetched = rates.get(model_id)
        if fetched is None:
            report.missing_from_source.append(model_id)
        else:
            if (
                fetched.input_usd_per_mtok == entry["input_usd_per_mtok"]
                and fetched.output_usd_per_mtok == entry["output_usd_per_mtok"]
            ):
                report.unchanged.append(model_id)
            else:
                report.updated.append(
                    f"{model_id} (in {entry['input_usd_per_mtok']}->"
                    f"{fetched.input_usd_per_mtok}, out {entry['output_usd_per_mtok']}->"
                    f"{fetched.output_usd_per_mtok})"
                )
                entry["input_usd_per_mtok"] = fetched.input_usd_per_mtok
                entry["output_usd_per_mtok"] = fetched.output_usd_per_mtok
        merged[model_id] = entry

    for model_id, fetched in sorted(rates.items()):
        if model_id in merged:
            continue
        merged[model_id] = {
            "input_usd_per_mtok": fetched.input_usd_per_mtok,
            "output_usd_per_mtok": fetched.output_usd_per_mtok,
        }
        report.added.append(model_id)
        report.needs_threshold.append(model_id)

    report.multiplier_notes.extend(_check_multipliers(rates, current))
    return _render_toml(merged, current, report), report


def refresh(
    *,
    destination: Path | None = None,
    source_url: str = DEFAULT_SOURCE_URL,
    source_file: Path | None = None,
    today: str | None = None,
    dry_run: bool = False,
) -> RefreshReport:
    """Fetch, merge, and write the user's pricing table. Returns what changed."""
    target = destination or user_pricing_path()
    if source_file is not None:
        payload = read_source_file(source_file)
        source_name = str(source_file)
    else:
        payload = fetch_source(source_url)
        source_name = source_url

    # Merge onto whatever is currently in effect, so a second refresh builds on the first.
    current = load_pricing(target) if target.is_file() else load_pricing(BUNDLED_PRICING_PATH)
    text, report = build_table(
        payload,
        current,
        source_name=source_name,
        today=today or datetime.now(UTC).date().isoformat(),
    )
    if dry_run:
        return report

    target.parent.mkdir(parents=True, exist_ok=True)
    # Write via a sibling temp file and replace, so an interrupted refresh cannot leave a
    # half-written rate table that would price a session with a truncated model list.
    temp = target.with_suffix(".toml.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(target)
    clear_pricing_cache()  # a later load in this process must see the new rates, not the old
    return report


@dataclass(frozen=True)
class _FetchedRate:
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_write_usd_per_mtok: float | None
    cache_read_usd_per_mtok: float | None


def _anthropic_rates(source: dict[str, object]) -> dict[str, _FetchedRate]:
    """Extract Anthropic per-token rates and normalize them to USD per million tokens."""
    rates: dict[str, _FetchedRate] = {}
    for raw_id, entry in source.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("litellm_provider") != "anthropic":
            continue
        model_id = _normalize_model_id(str(raw_id))
        if not model_id.startswith("claude-"):
            continue
        input_cost = _as_float(entry.get("input_cost_per_token"))
        output_cost = _as_float(entry.get("output_cost_per_token"))
        if input_cost is None or output_cost is None:
            continue
        rates[model_id] = _FetchedRate(
            input_usd_per_mtok=_per_token_to_per_mtok(input_cost),
            output_usd_per_mtok=_per_token_to_per_mtok(output_cost),
            cache_write_usd_per_mtok=_optional_per_mtok(
                _as_float(entry.get("cache_creation_input_token_cost"))
            ),
            cache_read_usd_per_mtok=_optional_per_mtok(
                _as_float(entry.get("cache_read_input_token_cost"))
            ),
        )
    return rates


def _check_multipliers(rates: dict[str, _FetchedRate], current: Pricing) -> list[str]:
    """Compare the source's implied cache multipliers against the configured ones.

    Reported, never silently applied. The multipliers are a documented property of the API
    (1.25x / 2x write, 0.1x read); if a rate source disagrees, that is a finding for a human,
    not a number to overwrite the table with.
    """
    notes: list[str] = []
    configured_read = Decimal(current.cache.read.numerator) / Decimal(
        current.cache.read.denominator
    )
    configured_write = Decimal(current.cache.write_5m.numerator) / Decimal(
        current.cache.write_5m.denominator
    )
    read_mismatch: list[str] = []
    write_mismatch: list[str] = []
    for model_id, rate in sorted(rates.items()):
        if model_id not in current.models:
            continue
        base = Decimal(str(rate.input_usd_per_mtok))
        if base == 0:
            continue
        if rate.cache_read_usd_per_mtok is not None:
            implied = (Decimal(str(rate.cache_read_usd_per_mtok)) / base).quantize(Decimal("0.001"))
            if implied != configured_read.quantize(Decimal("0.001")):
                read_mismatch.append(f"{model_id}={implied}")
        if rate.cache_write_usd_per_mtok is not None:
            implied = (Decimal(str(rate.cache_write_usd_per_mtok)) / base).quantize(
                Decimal("0.001")
            )
            if implied != configured_write.quantize(Decimal("0.001")):
                write_mismatch.append(f"{model_id}={implied}")
    if read_mismatch:
        notes.append(
            f"cache read multiplier: source implies {', '.join(read_mismatch)} vs configured "
            f"{configured_read} — left unchanged; verify against the caching documentation"
        )
    if write_mismatch:
        notes.append(
            f"cache write (5m) multiplier: source implies {', '.join(write_mismatch)} vs "
            f"configured {configured_write} — left unchanged; verify against the documentation"
        )
    return notes


def _render_toml(
    models: dict[str, dict[str, object]], current: Pricing, report: RefreshReport
) -> str:
    """Render the merged table. Hand-written rather than via a TOML writer: the file is meant
    to be read and edited by a person, and the comments are load-bearing."""
    lines = [
        "# Pricing, cache multipliers, and cacheability minimums — the one place these live.",
        "#",
        "# GENERATED by `ccost pricing refresh`, then yours to edit. This file lives under",
        "# CCOST_HOME and survives tool upgrades, so a rate you correct here stays corrected.",
        "#",
        "# Every figure ccost reports is API-EQUIVALENT COST imputed from these rates — never",
        "# a billed amount (FR-010).",
        "#",
        f"# Rates from: {report.source}",
        f"# Fetched:    {report.fetched_on}",
        "#",
        "# `min_cacheable_tokens` is NOT published by rate sources and is preserved across",
        "# refreshes. It must be filled in by hand from the prompt-caching documentation, and it",
        "# is NOT derivable from the model ordering — it does not decrease monotonically across",
        "# generations (Opus 4.7 needs 2048; the newer Opus 5 needs 512). A model without it",
        "# raises rather than being lane-classified against a guess.",
        "",
        "schema_version = 1",
        f'priced_on = "{report.fetched_on}"',
        "",
        "[cache]",
        "# Multipliers on the model's base input rate. Verified against, not overwritten by, the",
        "# rate source — see the refresh output for any divergence it reported.",
        f"read_multiplier = {_fraction_text(current.cache.read)}",
        f"write_multiplier_5m = {_fraction_text(current.cache.write_5m)}",
        f"write_multiplier_1h = {_fraction_text(current.cache.write_1h)}",
        f"unknown_ttl_multiplier = {_fraction_text(current.cache.unknown_ttl)}",
        f'unknown_ttl_confidence = "{current.cache.unknown_ttl_confidence}"',
        "",
    ]
    for model_id in sorted(models):
        entry = models[model_id]
        lines.append(f'[models."{model_id}"]')
        lines.append(f"input_usd_per_mtok = {entry['input_usd_per_mtok']}")
        lines.append(f"output_usd_per_mtok = {entry['output_usd_per_mtok']}")
        if "min_cacheable_tokens" in entry:
            lines.append(f"min_cacheable_tokens = {entry['min_cacheable_tokens']}")
        else:
            lines.append("# min_cacheable_tokens = ?  # REQUIRED — look it up; do not guess.")
        lines.append("")
    return "\n".join(lines)


def _normalize_model_id(raw: str) -> str:
    """Strip provider prefixes so a source's key matches the id recorded in a transcript."""
    for separator in ("/", "."):
        if separator in raw:
            head, _, tail = raw.rpartition(separator)
            if head:
                raw = tail
    return raw


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _per_token_to_per_mtok(per_token: float) -> float:
    """Convert a per-token USD rate to USD per million tokens, exactly."""
    return float(Decimal(str(per_token)) * TOKENS_PER_MTOK)


def _optional_per_mtok(per_token: float | None) -> float | None:
    return None if per_token is None else _per_token_to_per_mtok(per_token)


def _micros_per_mtok_to_usd(micros: int) -> float:
    return float(Decimal(micros) / MICROS_PER_DOLLAR)


def _fraction_text(value: Fraction) -> str:
    """Render an exact multiplier back to the decimal a human would recognise (5/4 -> 1.25)."""
    return str(Decimal(value.numerator) / Decimal(value.denominator))
