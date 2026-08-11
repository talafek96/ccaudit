"""The configuration registry — one source per registry, loaded once (Principle IX).

Everything a call site would otherwise hardcode lives behind this loader: token rates, the
TTL-dependent cache-write multipliers, and the per-model minimum cacheable prefix.

The threshold table is **load-bearing, not a convenience**. The minimum cacheable prefix is
not monotonic across model generations — Opus 4.7 requires 2048 while the newer Opus 5
requires 512 — so it cannot be derived from a version ordering and must be looked up
(docs/cost-model.md §2).

**Rates are not pinned to the tool's version.** Prices, cache multipliers, and thresholds all
drift, and chasing them with a release would make every installed copy quietly stale. So the
table resolves in this order, most specific first:

1. ``$CCAUDIT_PRICING`` — an explicit path, for tests and for a team-managed table.
2. ``$CCAUDIT_HOME/pricing.toml`` — the user's own table. Written by ``ccaudit pricing
   refresh`` and **survives tool upgrades**, so a refreshed rate outlives the release that
   shipped alongside it.
3. The bundled table — a dated seed, never the thing that keeps a machine current.

The refresh is explicit and opt-in (:mod:`ccaudit.config.refresh`); analysis itself never
touches the network, so the offline guarantee (FR-029, FR-030, SC-011) is intact. Every
result records which table priced it, so a reader can see how old the rates are.
"""

import os
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from ccaudit.config.components import (
    ATTRIBUTION_COMPONENTS,
    CHARGE_COMPONENTS,
    CostComponent,
    attribution_component,
    charge_component,
    sig_figs_for,
)
from ccaudit.money import to_fraction, usd_to_micros

__all__ = [
    "ATTRIBUTION_COMPONENTS",
    "BUNDLED_PRICING_PATH",
    "CHARGE_COMPONENTS",
    "STALE_RATES_AFTER_DAYS",
    "CacheRates",
    "CostComponent",
    "MissingThresholdError",
    "ModelPricing",
    "Pricing",
    "UnknownModelError",
    "attribution_component",
    "ccaudit_home",
    "charge_component",
    "clear_pricing_cache",
    "load_pricing",
    "resolve_pricing_path",
    "sig_figs_for",
    "user_pricing_path",
]

BUNDLED_PRICING_PATH = Path(__file__).with_name("pricing.toml")
PRICING_PATH_ENV = "CCAUDIT_PRICING"
CCAUDIT_HOME_ENV = "CCAUDIT_HOME"
USER_PRICING_FILENAME = "pricing.toml"

# When a rate table stops being safe to assume current. Model prices move a few times a year,
# so a table older than this is not necessarily wrong — it is unverified, which is a different
# claim and one the reader is entitled to see. Nothing refreshes on its own: an automatic
# update would put a network call on the analysis path and make two runs over the same
# transcript disagree (FR-017).
STALE_RATES_AFTER_DAYS = 90

# Claude Code records models in a few decorated forms. These are presentation suffixes on the
# same priced model, not distinct models, so they normalize away before lookup.
_CONTEXT_SUFFIXES: tuple[str, ...] = ("[1m]", "[1M]", "-1m", "-200k", "-1024k")
_VENDOR_PREFIXES: tuple[str, ...] = ("anthropic.", "anthropic/", "us.anthropic.", "eu.anthropic.")


class UnknownModelError(KeyError):
    """Raised when a rate is requested for a model that is not in the pricing table.

    Deliberately fatal (Principle I). Defaulting to a neighbouring model's rate would produce
    a plausible, confidently wrong figure — the exact failure this tool exists to prevent.

    ``model`` carries the offending id so a caller sweeping a whole corpus can name the session
    it skipped without parsing the message. ``~/.claude`` holds sessions from other tools too,
    and one of those must not be able to kill a run over everything else.
    """

    def __init__(self, message: str, *, model: str) -> None:
        super().__init__(message)
        self.model = model


class MissingThresholdError(KeyError):
    """Raised when a model's minimum cacheable prefix is not recorded.

    A refreshed table can gain a model whose threshold the rate source does not publish. We
    refuse to invent one: guessing it wrong misprices exactly the small instruction files this
    tool exists to price, by a factor of ten, in the direction that hides the error (FR-019).
    """


@dataclass(frozen=True)
class ModelPricing:
    """Everything priced per model. ``min_cacheable_tokens`` is looked up, never derived.

    ``min_cacheable_tokens`` is ``None`` when the table does not record it — an honest gap,
    not a default. Reading it then raises rather than returning a plausible number.
    """

    model_id: str
    input_micros_per_mtok: int
    output_micros_per_mtok: int
    min_cacheable_tokens: int | None

    @property
    def fingerprint(self) -> str:
        """Every value of this row that can change a figure, in a stable form."""
        return (
            f"{self.input_micros_per_mtok}/{self.output_micros_per_mtok}/"
            f"{self.min_cacheable_tokens}"
        )


@dataclass(frozen=True)
class CacheRates:
    """Lane multipliers applied to a model's base input rate (docs/cost-model.md §1)."""

    read: Fraction
    write_5m: Fraction
    write_1h: Fraction
    unknown_ttl: Fraction
    unknown_ttl_confidence: str

    @property
    def fingerprint(self) -> str:
        """Every multiplier that can change a figure. Fractions render exactly, unlike floats."""
        return (
            f"{self.read}/{self.write_5m}/{self.write_1h}/"
            f"{self.unknown_ttl}/{self.unknown_ttl_confidence}"
        )

    def write_multiplier(self, ttl: str | None) -> tuple[Fraction, str | None]:
        """Write multiplier for a recorded TTL, plus a confidence ceiling when it is unknown.

        A single session-wide write multiplier is wrong: the TTL doubles the rate. Where the
        record does not say which window applied, we price at 5m and cap the figure's
        confidence rather than assume (FR-080, docs/cost-model.md §5.5).
        """
        if ttl in ("5m", "5min", "ephemeral"):
            return self.write_5m, None
        if ttl in ("1h", "60m"):
            return self.write_1h, None
        return self.unknown_ttl, self.unknown_ttl_confidence


@dataclass(frozen=True)
class Pricing:
    """The loaded pricing registry."""

    schema_version: int
    priced_on: str
    source_path: Path
    source_origin: str
    cache: CacheRates
    models: dict[str, ModelPricing]

    @property
    def fingerprint(self) -> str:
        """Identity of the *rates themselves*, for deciding whether a cached figure still holds.

        Rates are refreshable at any time (FR-099), so a figure priced by a superseded table is
        a wrong number rather than an old one. That makes this part of every cache key
        (FR-106). Derived from the values that enter a calculation — not from the file's bytes,
        which would also change on a comment edit, and not from `priced_on`, which a table can
        carry without its rates having moved.
        """
        parts = [f"cache:{self.cache.fingerprint}"]
        for model_id in sorted(self.models):
            model = self.models[model_id]
            parts.append(f"{model_id}:{model.fingerprint}")
        return sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    @property
    def provenance(self) -> str:
        """One line naming the table and its date, for every surface that shows a figure.

        Rates drift, so "which table priced this, and when" is part of the number's basis —
        not a footnote (FR-014, FR-018).
        """
        return f"{self.source_origin} pricing table, rates as published {self.priced_on}"

    def age_days(self, today: date | None = None) -> int | None:
        """How old these rates are, or ``None`` if the table does not say.

        Nothing refreshes automatically — that would make two runs over the same transcript
        disagree, which FR-017 forbids, and would put a network call on the analysis path. So
        staleness is *reported* and the user decides.
        """
        try:
            published = date.fromisoformat(self.priced_on)
        except ValueError:
            return None
        reference = today or datetime.now(UTC).date()
        return (reference - published).days

    def staleness_note(self, today: date | None = None) -> str | None:
        """A warning when the rates are old enough to be worth re-checking, else ``None``.

        Model prices change a few times a year, so ``STALE_RATES_AFTER_DAYS`` is the point at
        which "these were current when I installed it" stops being a safe assumption. An old
        table is not wrong — it is *unverified*, and the difference is the reader's to judge.
        """
        age = self.age_days(today)
        if age is None:
            return (
                f"The rate table at {self.source_path} does not record when its rates were "
                f"published, so how current they are cannot be judged. Run "
                f"`ccaudit pricing refresh` to replace it with a dated one."
            )
        if age < STALE_RATES_AFTER_DAYS:
            return None
        return (
            f"These rates were published {age} days ago ({self.priced_on}) and have not been "
            f"checked since. Prices change a few times a year; run `ccaudit pricing refresh` "
            f"to update them without upgrading ccaudit."
        )

    def for_model(self, model_id: str) -> ModelPricing:
        """Resolve a model's rates, raising rather than defaulting on an unknown id."""
        for candidate in _normalization_candidates(model_id):
            if candidate in self.models:
                return self.models[candidate]
        raise UnknownModelError(
            f"no pricing for model {model_id!r} in {self.source_path}. "
            f"Known models: {sorted(self.models)}. Add the model to pricing.toml — including "
            f"its min_cacheable_tokens, which must be looked up rather than guessed from the "
            f"version ordering (it is not monotonic).",
            model=model_id,
        )

    def min_cacheable_tokens(self, model_id: str) -> int:
        """The minimum cacheable prefix for this model. Below it, content is billed at full
        rate on every turn with no error and no cache-creation tokens (FR-078, FR-079)."""
        model = self.for_model(model_id)
        if model.min_cacheable_tokens is None:
            raise MissingThresholdError(
                f"{self.source_path} has no min_cacheable_tokens for {model.model_id!r}. "
                f"Rate sources do not publish it, so it must be filled in by hand from the "
                f"prompt-caching documentation. It is NOT derivable from the model ordering — "
                f"Opus 4.7 requires 2048 while the newer Opus 5 requires 512."
            )
        return model.min_cacheable_tokens


def user_pricing_path() -> Path:
    """Where ``ccaudit pricing refresh`` writes, and where a refreshed table is read from.

    Under ``CCAUDIT_HOME`` rather than inside the installed package, so it survives an upgrade
    and is not owned by the release.
    """
    return ccaudit_home() / USER_PRICING_FILENAME


def ccaudit_home() -> Path:
    """The per-user state directory, created on demand — no setup step (FR-050)."""
    override = os.environ.get(CCAUDIT_HOME_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "ccaudit"
    return Path.home() / ".local" / "state" / "ccaudit"


def resolve_pricing_path() -> tuple[Path, str]:
    """Pick the pricing table to use, and say where it came from.

    Explicit path, then the user's refreshable table, then the bundled seed. The bundled
    table is a starting point, never the mechanism that keeps a machine current.
    """
    explicit = os.environ.get(PRICING_PATH_ENV)
    if explicit:
        return Path(explicit).expanduser(), "explicit ($CCAUDIT_PRICING)"
    user_path = user_pricing_path()
    if user_path.is_file():
        return user_path, "refreshed"
    return BUNDLED_PRICING_PATH, "bundled"


def load_pricing(path: Path | None = None) -> Pricing:
    """Load the pricing registry from the resolved table.

    Cached per resolved path so a run parses the table once; pass an explicit path in tests.
    """
    if path is not None:
        return _load_pricing_cached(path.resolve(), "explicit (argument)")
    resolved, origin = resolve_pricing_path()
    return _load_pricing_cached(resolved.resolve(), origin)


def clear_pricing_cache() -> None:
    """Drop the parsed-table cache, so a refresh mid-process takes effect immediately."""
    _load_pricing_cached.cache_clear()


@lru_cache(maxsize=8)
def _load_pricing_cached(path: Path, origin: str) -> Pricing:
    if not path.is_file():
        raise FileNotFoundError(
            f"pricing configuration not found at {path}. Set {PRICING_PATH_ENV} to point at it."
        )
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    try:
        cache_raw = raw["cache"]
        models_raw = raw["models"]
        schema_version = int(raw["schema_version"])
    except KeyError as exc:
        raise ValueError(f"{path} is missing required key {exc.args[0]!r}") from None

    cache = CacheRates(
        read=to_fraction(cache_raw["read_multiplier"]),
        write_5m=to_fraction(cache_raw["write_multiplier_5m"]),
        write_1h=to_fraction(cache_raw["write_multiplier_1h"]),
        unknown_ttl=to_fraction(cache_raw["unknown_ttl_multiplier"]),
        unknown_ttl_confidence=str(cache_raw["unknown_ttl_confidence"]),
    )

    models: dict[str, ModelPricing] = {}
    for model_id, entry in models_raw.items():
        missing = {"input_usd_per_mtok", "output_usd_per_mtok"} - set(entry)
        if missing:
            raise ValueError(f"{path}: model {model_id!r} is missing {sorted(missing)}")
        threshold = entry.get("min_cacheable_tokens")
        models[model_id] = ModelPricing(
            model_id=model_id,
            input_micros_per_mtok=usd_to_micros(entry["input_usd_per_mtok"]),
            output_micros_per_mtok=usd_to_micros(entry["output_usd_per_mtok"]),
            min_cacheable_tokens=None if threshold is None else int(threshold),
        )
    if not models:
        raise ValueError(f"{path} defines no models")

    return Pricing(
        schema_version=schema_version,
        priced_on=str(raw.get("priced_on", "unknown")),
        source_path=path,
        source_origin=origin,
        cache=cache,
        models=models,
    )


def _normalization_candidates(model_id: str) -> list[str]:
    """Forms of a recorded model id to try, most specific first.

    Transcripts carry decorated ids — a dated snapshot (``claude-haiku-4-5-20251001``), a
    context-window marker (``claude-opus-5[1m]``), or a cloud vendor prefix. All name the same
    priced model, so they are stripped rather than added as duplicate table rows.
    """
    candidates: list[str] = []

    def push(value: str) -> None:
        if value and value not in candidates:
            candidates.append(value)

    push(model_id)
    stripped = model_id.strip()
    push(stripped)

    for prefix in _VENDOR_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]
            push(stripped)
            break

    for suffix in _CONTEXT_SUFFIXES:
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
            push(stripped)
            break

    # Dated snapshot: trailing -YYYYMMDD.
    head, sep, tail = stripped.rpartition("-")
    if sep and len(tail) == 8 and tail.isdigit():
        push(head)

    return candidates
