"""Contract on the carry-splitting policies.

The property that matters is conservation: whatever a policy does with a pool, the pieces plus
the unattributed remainder must equal the pool exactly. A policy that leaks a micro-dollar
breaks the session-level invariant for the whole report.
"""

import pytest

from ccaudit.model.policy import (
    DEFAULT_POLICY,
    POLICIES,
    UnknownPolicyError,
    describe,
    split_pool,
)


class TestConservation:
    @pytest.mark.parametrize("policy", POLICIES)
    @pytest.mark.parametrize(
        ("pool", "weights"),
        [
            (1_000_000, [1, 1, 1]),
            (1, [1, 1, 1, 1, 1, 1, 1]),
            (999_999_999, [984, 51_100, 24_300]),
            (7, [1_000_000, 1, 1]),
            (0, [5, 5]),
            (13, [0, 0]),
            (500, [42]),
            (500, []),
        ],
    )
    def test_every_policy_conserves_its_pool(
        self, policy: str, pool: int, weights: list[int]
    ) -> None:
        split = split_pool(pool, weights, policy=policy)
        assert sum(split.shares) + split.unattributed == pool

    @pytest.mark.parametrize("policy", POLICIES)
    def test_share_count_matches_item_count(self, policy: str) -> None:
        split = split_pool(100, [1, 2, 3], policy=policy)
        assert len(split.shares) == 3


class TestProportional:
    def test_is_the_default(self) -> None:
        """Chosen for auditability: explainable in a sentence, recomputable by hand."""
        assert DEFAULT_POLICY == "proportional"

    def test_splits_by_token_weight(self) -> None:
        split = split_pool(300, [1, 2, 3], policy="proportional")
        assert split.shares == (50, 100, 150)
        assert split.unattributed == 0

    def test_a_disputant_can_recompute_it_by_hand(self) -> None:
        """90 micro-dollars across a 1:2 resident set is 30 and 60. That is the whole rule."""
        split = split_pool(90, [1_000, 2_000], policy="proportional")
        assert split.shares == (30, 60)


class TestExclusive:
    def test_a_lone_resident_item_gets_the_whole_pool(self) -> None:
        split = split_pool(500, [42], policy="exclusive")
        assert split.shares == (500,)
        assert split.unattributed == 0

    def test_shared_cost_becomes_unattributed_rather_than_divided(self) -> None:
        """The point of this policy: attribute less, visibly, instead of by a disputed rule."""
        split = split_pool(500, [10, 20, 30], policy="exclusive")
        assert split.shares == (0, 0, 0)
        assert split.unattributed == 500

    def test_it_attributes_less_than_proportional_not_differently_total(self) -> None:
        proportional = split_pool(1_000, [5, 5], policy="proportional")
        exclusive = split_pool(1_000, [5, 5], policy="exclusive")
        assert sum(proportional.shares) > sum(exclusive.shares)
        assert proportional.total == exclusive.total == 1_000


class TestNoResidentItems:
    @pytest.mark.parametrize("policy", POLICIES)
    def test_cost_with_nothing_to_explain_it_is_named_not_spread(self, policy: str) -> None:
        """FR-013 — the remainder is never distributed across attributable items."""
        split = split_pool(750, [], policy=policy)
        assert split.shares == ()
        assert split.unattributed == 750


class TestFailFast:
    def test_an_unknown_policy_raises_rather_than_defaulting(self) -> None:
        with pytest.raises(UnknownPolicyError, match="unknown carry-splitting policy"):
            split_pool(100, [1], policy="shapley")

    def test_negative_weights_raise(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            split_pool(100, [1, -1])

    def test_describe_raises_for_an_unknown_policy(self) -> None:
        with pytest.raises(UnknownPolicyError):
            describe("vibes")


class TestDescription:
    @pytest.mark.parametrize("policy", POLICIES)
    def test_every_policy_explains_itself_briefly_and_plainly(self, policy: str) -> None:
        """It appears next to the figures it produced, wherever totals are shown (FR-097).

        Short enough to sit under a total without pushing the numbers off the screen, and
        written in words a non-expert reads correctly — no "amortized", no "dominator".
        """
        sentence = describe(policy)
        assert sentence.endswith(".")
        assert len(sentence) < 200
        assert "proportion" in sentence or "shared" in sentence
