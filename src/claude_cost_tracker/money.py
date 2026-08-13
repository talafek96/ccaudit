"""Integer micro-dollar arithmetic — the leaf primitive under every figure this tool prints.

This module sits outside the ``config -> ingest -> model -> render`` layering on purpose:
it has no imports of its own, and all three layers need it (rates to cost, largest-remainder
splits, significant figures at the presentation edge). Duplicating money arithmetic across
layers is precisely how Invariant A1 breaks.

**Why integers, not floats.** Not precision — conservation. SC-001 requires

    sum(per-item) + unattributed == session total

to be a literal equality. With floats that comparison can be False from a 1e-16 ordering
artifact on a perfectly correct breakdown, and the fix would be a tolerance — at which point
the epsilon becomes the place real errors hide. Integers make the check exact and need no
epsilon. See specs/001-per-file-cost-attribution/research.md §8.

**Exact is not accurate.** The arithmetic here conserves exactly; the inputs are imputed
list prices and a splitting policy. :func:`format_micros` exists so that internal exactness
is never displayed as precision the figures do not have (FR-095, FR-098).
"""

from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

MICROS_PER_DOLLAR = 1_000_000
TOKENS_PER_MTOK = 1_000_000


def usd_to_micros(usd: float | str | Decimal) -> int:
    """Convert a human-written USD amount from config into integer micro-dollars.

    Goes through ``Decimal`` so ``0.1`` means a tenth of a cent, not its binary neighbour.
    """
    return int((Decimal(str(usd)) * MICROS_PER_DOLLAR).to_integral_value(ROUND_HALF_EVEN))


def to_fraction(value: float | str | Decimal) -> Fraction:
    """Convert a config multiplier into an exact rational (1.25 -> 5/4, not 1.2500000000000002)."""
    return Fraction(Decimal(str(value)))


def cost_micros(tokens: int, rate_micros_per_mtok: int, multiplier: Fraction = Fraction(1)) -> int:
    """Cost of ``tokens`` at ``rate_micros_per_mtok``, scaled by a cache-lane ``multiplier``.

    The whole computation is done in exact rationals and rounded **once**, at the end, so a
    1.25x write and a 0.1x read never accumulate a rounding drift between them.

    Raises on a negative token count: a negative charge is a broken invariant upstream, not
    a value any caller should branch on (Principle I).
    """
    if tokens < 0:
        raise ValueError(f"token count must be non-negative, got {tokens}")
    if rate_micros_per_mtok < 0:
        raise ValueError(f"rate must be non-negative, got {rate_micros_per_mtok}")
    exact = Fraction(tokens * rate_micros_per_mtok, TOKENS_PER_MTOK) * multiplier
    return _round_half_even(exact)


def allocate(total_micros: int, weights: list[int]) -> list[int]:
    """Split ``total_micros`` across ``weights``, conserving the total **exactly**.

    Largest-remainder allocation: each bucket gets its floor share, then the leftover units
    go one each to the buckets with the largest discarded fractions, ties broken by position
    so the result is deterministic (FR-017, SC-009). This is Invariant A3 — a proportional
    split that drops its remainder fails Invariant A1 by construction.

    ``sum(allocate(t, w)) == t`` holds for every input, including all-zero weights (the
    remainder then lands on the earliest buckets) and an empty bucket list only when the
    total is zero.
    """
    if any(w < 0 for w in weights):
        raise ValueError(f"weights must be non-negative, got {weights}")
    if not weights:
        if total_micros != 0:
            raise ValueError(f"cannot allocate {total_micros} micro-dollars across zero buckets")
        return []

    # A negative pool is legitimate only as a sign-flipped split; handle it by allocating the
    # magnitude and negating, so the conservation guarantee holds in both directions.
    if total_micros < 0:
        return [-share for share in allocate(-total_micros, weights)]

    total_weight = sum(weights)
    if total_weight == 0:
        # Nothing to be proportional to. Spread evenly rather than inventing a preference,
        # and let largest-remainder place the leftover units deterministically.
        weights = [1] * len(weights)
        total_weight = len(weights)

    shares = [total_micros * w // total_weight for w in weights]
    remainder = total_micros - sum(shares)
    if remainder:
        # Rank by the discarded fractional part, descending; position breaks ties.
        order = sorted(
            range(len(weights)),
            key=lambda i: (-((total_micros * weights[i]) % total_weight), i),
        )
        for i in order[:remainder]:
            shares[i] += 1
    return shares


def format_axis_micros(micros: int) -> str:
    """Render a money value as an **axis tick**, which is a different job from a figure.

    `format_micros` collapses anything under half a cent to "<$0.01", which is the honest thing
    to do for a *figure*: below that, the precision is not warranted. A scale mark is not a
    figure — it is the ruler the figures are read against — and a ruler whose marks cannot be
    told apart is not a ruler. On a log axis spanning five decades, three of them fall below a
    cent and every one of their labels would read "<$0.01".

    So ticks keep as many decimals as the value needs, and no more. This is the only place that
    diverges from `format_micros`, and it diverges on the axis only.
    """
    dollars = Decimal(micros) / MICROS_PER_DOLLAR
    if dollars == 0:
        return "$0"
    sign = "-" if dollars < 0 else ""
    magnitude = abs(dollars)
    if magnitude >= 1:
        return f"{sign}${magnitude.normalize():f}"
    # Enough decimals to show the leading significant digit of a sub-dollar value.
    decimals = -magnitude.adjusted()
    return f"{sign}${magnitude.quantize(Decimal(1).scaleb(-decimals)):f}"


def format_micros(micros: int, sig_figs: int) -> str:
    """Render micro-dollars as a USD string carrying no more precision than is warranted.

    ``sig_figs`` comes from the figure's confidence level, never from the caller's taste
    (FR-095). Sub-cent figures are shown as ``<$0.01`` rather than as a false zero.
    """
    if sig_figs < 1:
        raise ValueError(f"sig_figs must be at least 1, got {sig_figs}")
    dollars = Decimal(micros) / MICROS_PER_DOLLAR
    if dollars == 0:
        return "$0.00"

    sign = "-" if dollars < 0 else ""
    magnitude = abs(dollars)
    if magnitude < Decimal("0.005"):
        return f"{sign}<$0.01"

    # Round to `sig_figs` significant figures, but never to coarser than cents: a figure of
    # $1234.56 at 2 sig figs reads "$1200", while $0.37 at 2 sig figs must stay "$0.37".
    exponent = magnitude.adjusted()  # floor(log10(magnitude))
    decimals = max(0, min(2, sig_figs - 1 - exponent))
    quantum = Decimal(1).scaleb(-decimals)
    rounded = magnitude.quantize(quantum, rounding=ROUND_HALF_EVEN)
    if decimals == 0 and sig_figs - 1 - exponent < 0:
        # Coarser than the units place: zero out the digits the confidence cannot support.
        step = Decimal(1).scaleb(exponent - sig_figs + 1)
        rounded = (rounded / step).quantize(Decimal(1), rounding=ROUND_HALF_EVEN) * step
    return f"{sign}${rounded:,.{decimals}f}"


def format_share(share: float) -> str:
    """Render a share of total as a percentage. Every absolute is paired with one (FR-011)."""
    if share >= 0.001 or share <= 0:
        return f"{share * 100:.1f}%"
    return "<0.1%"


def _round_half_even(value: Fraction) -> int:
    """Round an exact rational to the nearest integer, halves to even.

    Banker's rounding rather than half-up so that splitting a pool into many small pieces
    does not systematically inflate the sum.
    """
    floor_value = value.numerator // value.denominator
    remainder = value - floor_value
    if remainder < Fraction(1, 2):
        return floor_value
    if remainder > Fraction(1, 2):
        return floor_value + 1
    return floor_value if floor_value % 2 == 0 else floor_value + 1
