"""Carry-splitting policies — how shared cost is divided among concurrently resident items.

Carry cost is genuinely *shared*: a turn re-shows everything resident, and the charge is one
number for the whole set. Dividing it among the items is therefore a **choice**, not a
measurement, and it is the single largest source of uncertainty in any per-item figure.

That is why the policy is a knob rather than a constant (FR-006). A hardcoded choice gets
re-litigated the first time someone disputes a number, and the tool exists to settle disputes.

Two policies ship, and they answer two different questions:

- **proportional** (default) — "what share of the space was this?" Splits by token weight.
  Chosen for auditability above all: it is explainable in one sentence and a disputant can
  recompute it by hand from figures the report already shows.
- **exclusive** — "what would definitely go away if this item did?" Charges an item only for
  cost it alone caused; everything shared lands in the unattributed remainder instead. It
  attributes *less*, visibly, rather than dividing by a rule someone can argue with.

Whichever is in effect, **the total never moves.** A policy redistributes per-item figures
within a fixed pool; it cannot change what the session cost.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ccaudit.money import allocate

POLICIES: tuple[str, ...] = ("proportional", "exclusive")
DEFAULT_POLICY = "proportional"

POLICY_DESCRIPTIONS: dict[str, str] = {
    "proportional": (
        "Shared carry cost is divided among resident items in proportion to how much space "
        "each occupies."
    ),
    "exclusive": (
        "An item is charged only for carry cost it alone caused. Cost shared between items is "
        "reported as unattributed rather than divided."
    ),
}


class UnknownPolicyError(ValueError):
    """Raised for a policy name that is not implemented — never silently defaulted."""


@dataclass(frozen=True)
class Split:
    """The result of dividing one pool.

    ``unattributed`` is a first-class part of the answer, not an error term: under the
    exclusive policy it is where shared cost is *supposed* to land (FR-012, FR-013).
    """

    shares: tuple[int, ...]
    unattributed: int

    def __post_init__(self) -> None:
        if self.unattributed < 0:
            raise ValueError(f"unattributed cannot be negative, got {self.unattributed}")

    @property
    def total(self) -> int:
        return sum(self.shares) + self.unattributed


def split_pool(pool_micros: int, weights: Sequence[int], policy: str = DEFAULT_POLICY) -> Split:
    """Divide ``pool_micros`` among items with the given token ``weights``.

    **Conservation is unconditional**: ``sum(shares) + unattributed == pool_micros`` for every
    policy and every input. A policy that leaked a micro-dollar would break Invariant A1 for
    the whole session, so this is asserted here rather than trusted downstream.
    """
    if policy not in POLICIES:
        raise UnknownPolicyError(
            f"unknown carry-splitting policy {policy!r}; known: {list(POLICIES)}"
        )
    if any(w < 0 for w in weights):
        raise ValueError(f"item weights must be non-negative, got {list(weights)}")

    if not weights:
        # Cost with no resident item to explain it. Named, not spread (FR-013).
        split = Split(shares=(), unattributed=pool_micros)
    elif policy == "proportional":
        split = Split(shares=tuple(allocate(pool_micros, list(weights))), unattributed=0)
    else:
        split = _split_exclusive(pool_micros, weights)

    if split.total != pool_micros:
        raise AssertionError(
            f"policy {policy!r} did not conserve its pool: {split.total} != {pool_micros}. "
            f"This would break the session-level reconciliation invariant."
        )
    return split


def _split_exclusive(pool_micros: int, weights: Sequence[int]) -> Split:
    """Charge only cost an item alone caused; report the shared part as unattributed.

    With one resident item the whole pool is exclusively its. With several, no item is the
    sole cause of any of it, so none of it is charged to an item — the honest answer under
    this policy, and the reason it exists as an alternative to proportional.
    """
    if len(weights) == 1:
        return Split(shares=(pool_micros,), unattributed=0)
    return Split(shares=tuple(0 for _ in weights), unattributed=pool_micros)


def describe(policy: str) -> str:
    """The one-sentence explanation shown wherever a policy-derived figure appears (FR-097)."""
    try:
        return POLICY_DESCRIPTIONS[policy]
    except KeyError:
        raise UnknownPolicyError(f"unknown carry-splitting policy {policy!r}") from None
