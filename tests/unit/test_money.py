"""Contract on the money primitive.

These tests exist because Invariant A1 (the breakdown adds up, exactly) is the product's core
promise, and it is enforced here or nowhere.
"""

from fractions import Fraction

import pytest

from ccaudit.money import (
    allocate,
    cost_micros,
    format_micros,
    format_share,
    to_fraction,
    usd_to_micros,
)


class TestUsdToMicros:
    def test_whole_dollars(self) -> None:
        assert usd_to_micros(5.0) == 5_000_000

    def test_sub_cent_precision_survives_the_float(self) -> None:
        # 0.1 has no exact binary representation; going through Decimal keeps it a tenth.
        assert usd_to_micros(0.1) == 100_000

    def test_accepts_a_string(self) -> None:
        assert usd_to_micros("2.50") == 2_500_000


class TestCostMicros:
    def test_five_dollars_per_mtok_is_five_micros_per_token(self) -> None:
        assert cost_micros(1_000_000, usd_to_micros(5.0)) == 5_000_000
        assert cost_micros(1, usd_to_micros(5.0)) == 5

    def test_cache_read_is_a_tenth(self) -> None:
        full = cost_micros(1_000_000, usd_to_micros(5.0))
        read = cost_micros(1_000_000, usd_to_micros(5.0), to_fraction(0.1))
        assert read * 10 == full

    def test_write_multipliers_differ_by_ttl(self) -> None:
        """The TTL doubles a cache write. A single session-wide multiplier is wrong."""
        rate = usd_to_micros(5.0)
        write_5m = cost_micros(1_000_000, rate, to_fraction(1.25))
        write_1h = cost_micros(1_000_000, rate, to_fraction(2.0))
        assert write_5m == 6_250_000
        assert write_1h == 10_000_000
        assert write_1h > write_5m

    def test_rounds_once_at_the_end(self) -> None:
        """Chained multipliers must not accumulate a rounding drift."""
        rate = usd_to_micros(3.0)
        assert cost_micros(7, rate, Fraction(1, 3)) == cost_micros(7, rate, Fraction(1, 3))
        # 7 tokens * 3 micros/token / 3 == 7 exactly, not 6 from an intermediate floor.
        assert cost_micros(7, rate, Fraction(1, 3)) == 7

    def test_zero_tokens_costs_nothing(self) -> None:
        assert cost_micros(0, usd_to_micros(5.0)) == 0

    def test_negative_tokens_raise(self) -> None:
        """A negative charge is a broken invariant upstream, not a value to branch on."""
        with pytest.raises(ValueError, match="non-negative"):
            cost_micros(-1, usd_to_micros(5.0))


class TestAllocate:
    @pytest.mark.parametrize(
        ("total", "weights"),
        [
            (100, [1, 1, 1]),
            (1, [1, 1, 1, 1, 1, 1, 1]),
            (999_999, [3, 5, 7, 11, 13]),
            (7, [1000000, 1, 1]),
            (0, [5, 5]),
            (13, [0, 0, 0]),
            (1_649_000_000, [984, 51_100, 24_300, 3, 1]),
        ],
    )
    def test_shares_always_sum_to_the_total(self, total: int, weights: list[int]) -> None:
        """Invariant A3. A proportional split that drops its remainder fails A1 by design."""
        shares = allocate(total, weights)
        assert sum(shares) == total
        assert len(shares) == len(weights)

    def test_proportionality_holds_where_it_can(self) -> None:
        assert allocate(100, [1, 1, 1, 1]) == [25, 25, 25, 25]
        assert allocate(90, [1, 2]) == [30, 60]

    def test_remainder_goes_to_the_largest_fraction(self) -> None:
        # 10 across [1,1,1]: 3 each, one unit left; largest-remainder ties break by position.
        assert allocate(10, [1, 1, 1]) == [4, 3, 3]

    def test_is_deterministic(self) -> None:
        """Same input, same figures — across runs and machines (FR-017, SC-009)."""
        weights = [17, 4, 4, 4, 9, 1]
        assert allocate(1_000_003, weights) == allocate(1_000_003, weights)

    def test_zero_weights_spread_evenly_rather_than_inventing_a_preference(self) -> None:
        assert sum(allocate(10, [0, 0, 0, 0])) == 10

    def test_negative_total_conserves_too(self) -> None:
        assert sum(allocate(-100, [1, 2, 3])) == -100

    def test_empty_buckets_with_a_nonzero_total_raise(self) -> None:
        with pytest.raises(ValueError, match="zero buckets"):
            allocate(5, [])

    def test_empty_buckets_with_zero_total_is_empty(self) -> None:
        assert allocate(0, []) == []

    def test_negative_weights_raise(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            allocate(10, [1, -1])


class TestFormatMicros:
    def test_high_confidence_shows_cents(self) -> None:
        assert format_micros(1_234_560_000, sig_figs=6) == "$1,234.56"

    def test_never_prints_finer_than_a_cent(self) -> None:
        """The arithmetic is exact to the micro-dollar; the display is not."""
        assert format_micros(1_234_567, sig_figs=6) == "$1.23"

    def test_low_confidence_does_not_show_cents(self) -> None:
        """Exactness is not accuracy: a policy-derived figure is not printed to the cent."""
        assert format_micros(1_234_560_000, sig_figs=2) == "$1,200"
        assert format_micros(1_234_560_000, sig_figs=1) == "$1,000"

    def test_small_figures_keep_cents_even_at_low_confidence(self) -> None:
        assert format_micros(370_000, sig_figs=2) == "$0.37"

    def test_sub_cent_is_not_reported_as_zero(self) -> None:
        assert format_micros(1_000, sig_figs=4) == "<$0.01"

    def test_exact_zero_is_zero(self) -> None:
        assert format_micros(0, sig_figs=4) == "$0.00"

    def test_negative_keeps_its_sign(self) -> None:
        assert format_micros(-5_000_000, sig_figs=4).startswith("-$")

    def test_sig_figs_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            format_micros(1_000_000, sig_figs=0)


class TestFormatShare:
    def test_shows_one_decimal(self) -> None:
        assert format_share(0.536) == "53.6%"

    def test_tiny_shares_are_not_rounded_to_zero(self) -> None:
        assert format_share(0.0001) == "<0.1%"

    def test_zero_is_zero(self) -> None:
        assert format_share(0.0) == "0.0%"
