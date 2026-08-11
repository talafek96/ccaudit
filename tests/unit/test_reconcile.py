"""Contract on the reconciliation invariant — the product's core promise.

Invariant A1 / SC-001: per-item figures plus the unattributed remainder equal the session
total, by integer equality with zero tolerance. These tests are the fence on that promise, so
they check the *failure* paths as hard as the success ones: a reconciliation that cannot fail
is not a reconciliation.
"""

import pytest

from ccaudit.model.attribute import Attribution
from ccaudit.model.reconcile import (
    ReconciliationError,
    assert_groups_reconcile,
    reconcile,
)


def attribution(cost: int, target_id: str = "file:-:/repo/a.py") -> Attribution:
    return Attribution(
        session_id="s1",
        turn_index=0,
        target_kind="item",
        target_id=target_id,
        component="carry",
        cost_micros=cost,
        basis="measured",
        confidence="medium",
    )


class TestReconcile:
    def test_a_complete_breakdown_leaves_no_remainder(self) -> None:
        result = reconcile([attribution(300), attribution(700)], total_micros=1_000)
        assert result.attributed_micros == 1_000
        assert result.unattributed_micros == 0
        assert result.adds_up

    def test_the_remainder_is_whatever_is_not_explained(self) -> None:
        result = reconcile([attribution(600)], total_micros=1_000)
        assert result.unattributed_micros == 400
        assert result.adds_up

    def test_it_adds_up_by_exact_integer_equality(self) -> None:
        """No epsilon. An epsilon is where a real misattribution would hide."""
        result = reconcile([attribution(333), attribution(333), attribution(333)], 1_000)
        assert result.attributed_micros + result.unattributed_micros == 1_000
        assert result.unattributed_micros == 1

    def test_a_session_with_no_attributions_is_entirely_unattributed(self) -> None:
        result = reconcile([], total_micros=5_000)
        assert result.unattributed_micros == 5_000
        assert result.adds_up

    def test_a_zero_cost_session_reconciles(self) -> None:
        result = reconcile([], total_micros=0)
        assert result.adds_up
        assert result.unattributed_share == 0.0


class TestOverAttribution:
    def test_attributing_more_than_the_total_raises(self) -> None:
        """The remainder absorbs a shortfall; it cannot absorb a surplus."""
        with pytest.raises(ReconciliationError, match="over-attribution"):
            reconcile([attribution(1_500)], total_micros=1_000)

    def test_the_failure_names_the_likely_cause(self) -> None:
        """A failure must carry enough to triage it (Principle I)."""
        with pytest.raises(ReconciliationError) as exc:
            reconcile([attribution(2_000)], total_micros=1_000)
        message = str(exc.value)
        assert "1000" in message.replace(",", "")
        assert "subagent" in message

    def test_passing_the_remainder_in_is_rejected_as_double_counting(self) -> None:
        remainder = Attribution(
            session_id="s1",
            turn_index=0,
            target_kind="unattributed",
            target_id=None,
            component="carry",
            cost_micros=100,
            basis="measured",
            confidence="low",
        )
        with pytest.raises(ReconciliationError, match="double-counts"):
            reconcile([attribution(500), remainder], total_micros=1_000)


class TestUnattributedShare:
    def test_the_share_is_always_available(self) -> None:
        """Displayed regardless of size — never hidden because it looks bad (FR-012)."""
        result = reconcile([attribution(910)], total_micros=1_000)
        assert result.unattributed_share == pytest.approx(0.09)

    def test_a_fully_unattributed_session_reports_a_share_of_one(self) -> None:
        result = reconcile([], total_micros=1_000)
        assert result.unattributed_share == 1.0


class TestGroupings:
    def test_a_grouping_that_partitions_the_attributions_passes(self) -> None:
        result = reconcile([attribution(300), attribution(700)], total_micros=1_000)
        assert_groups_reconcile([400, 600], result, scope="by folder")

    def test_a_grouping_that_drops_an_item_raises(self) -> None:
        """Every level of every breakdown adds up, not just the flat list (FR-007)."""
        result = reconcile([attribution(300), attribution(700)], total_micros=1_000)
        with pytest.raises(ReconciliationError, match="by folder"):
            assert_groups_reconcile([400], result, scope="by folder")

    def test_a_grouping_that_counts_an_item_twice_raises(self) -> None:
        result = reconcile([attribution(1_000)], total_micros=1_000)
        with pytest.raises(ReconciliationError, match="counted twice|partition"):
            assert_groups_reconcile([1_000, 1_000], result, scope="by category")

    def test_the_grouping_failure_names_the_difference(self) -> None:
        result = reconcile([attribution(1_000)], total_micros=1_000)
        with pytest.raises(ReconciliationError) as exc:
            assert_groups_reconcile([900], result, scope="by extension")
        assert "-100" in str(exc.value)


class TestAttributionInvariants:
    def test_output_cost_cannot_target_a_file(self) -> None:
        """Invariant A2 / FR-005 — output belongs to the exchange, not to a resident file."""
        with pytest.raises(ValueError, match="output cost cannot target an item"):
            Attribution(
                session_id="s1",
                turn_index=0,
                target_kind="item",
                target_id="file:-:/repo/a.py",
                component="output",
                cost_micros=100,
                basis="exact",
                confidence="high",
            )

    def test_output_may_target_the_exchange(self) -> None:
        assert (
            Attribution(
                session_id="s1",
                turn_index=0,
                target_kind="prompt",
                target_id=None,
                component="output",
                cost_micros=100,
                basis="exact",
                confidence="high",
            ).cost_micros
            == 100
        )

    def test_an_unknown_target_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown target kind"):
            Attribution(
                session_id="s1",
                turn_index=0,
                target_kind="vibes",
                target_id=None,
                component="carry",
                cost_micros=1,
                basis="measured",
                confidence="low",
            )
