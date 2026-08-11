"""The reconciliation invariant — the product's core promise, enforced in one place.

Invariant A1 (SC-001):

    sum(every attribution) + unattributed == the session total, exactly.

Integer equality, zero tolerance. There is deliberately no epsilon: the moment one exists it
becomes the place real errors hide, where a genuine misattribution slips under a threshold
chosen to absorb float noise. Integer micro-dollars make the check exact and need no epsilon
(research §8).

A violation **raises**. It is a defect in ccaudit, not in the user's session, and the tool
refuses to print rather than show a complete, plausible-looking breakdown of wrong numbers —
which is worse than no breakdown, because someone will act on it.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ccaudit.model.attribute import Attribution

# What every part-to-whole surface must show as its own visible entry (FR-012, FR-013).
UNATTRIBUTED_LABEL = "unattributed"
UNATTRIBUTED_DISPLAY = "couldn't attribute"


class ReconciliationError(AssertionError):
    """The breakdown does not add up. Maps to exit code 3.

    Defined here, where it is raised, rather than in the CLI: the model layer must not import
    from the layer above it. The CLI re-exports it for its exit-code mapping.

    An ``AssertionError`` on purpose — a breakdown that contradicts its own total is a broken
    invariant, not an outcome a caller should branch on.
    """


@dataclass(frozen=True)
class Reconciliation:
    """The checked result: attributed, unattributed, and the total they must sum to."""

    total_micros: int
    attributed_micros: int
    unattributed_micros: int

    @property
    def unattributed_share(self) -> float:
        """Always reported, however small — never hidden because it looks good (FR-012)."""
        if self.total_micros == 0:
            return 0.0
        return self.unattributed_micros / self.total_micros

    @property
    def adds_up(self) -> bool:
        return self.attributed_micros + self.unattributed_micros == self.total_micros


def reconcile(
    attributions: Iterable[Attribution],
    total_micros: int,
    *,
    scope: str = "session",
) -> Reconciliation:
    """Check the breakdown against its total and emit the remainder explicitly.

    The remainder is computed, not assumed: whatever the attributions do not explain becomes
    ``unattributed``. It is never distributed across the attributable items (FR-013), because
    spreading it would make every per-item figure quietly wrong in order to make the total
    look tidy.

    Raises :class:`~ccaudit.cli.ReconciliationError` if the arithmetic cannot be made to
    balance — which, given the remainder absorbs any shortfall, means an attribution exceeded
    the total or a figure was double-counted.
    """
    attributed = 0
    for attribution in attributions:
        if attribution.target_kind == UNATTRIBUTED_LABEL:
            raise ReconciliationError(
                f"{scope}: an unattributed row was passed in as an attribution. The remainder "
                f"is computed here, once, from what the attributions leave unexplained — "
                f"passing it in as well double-counts it."
            )
        attributed += attribution.cost_micros

    unattributed = total_micros - attributed
    if unattributed < 0:
        raise ReconciliationError(
            f"{scope}: attributed {attributed} micro-dollars against a total of "
            f"{total_micros} — an over-attribution of {-unattributed}. Every per-item figure "
            f"here is suspect; most likely a charge was counted under more than one item, or "
            f"a subagent turn was counted at both the child and the parent."
        )

    result = Reconciliation(
        total_micros=total_micros,
        attributed_micros=attributed,
        unattributed_micros=unattributed,
    )
    if not result.adds_up:  # pragma: no cover - unreachable by construction, kept as a fence
        raise ReconciliationError(
            f"{scope}: {result.attributed_micros} + {result.unattributed_micros} != "
            f"{result.total_micros}"
        )
    return result


def assert_groups_reconcile(
    group_totals: Sequence[int],
    reconciliation: Reconciliation,
    *,
    scope: str,
) -> None:
    """Check that a grouping (by folder, extension, category) still adds up.

    Every level of every breakdown reconciles, not just the flat per-file list (FR-007,
    FR-012). A grouping that silently drops an item is exactly the kind of error that looks
    right in a report.
    """
    grouped = sum(group_totals)
    expected = reconciliation.attributed_micros
    if grouped != expected:
        raise ReconciliationError(
            f"{scope}: grouped figures sum to {grouped} but the attributed total is "
            f"{expected} (difference {grouped - expected}). A grouping must partition the "
            f"attributions exactly — no item counted twice, none dropped."
        )
