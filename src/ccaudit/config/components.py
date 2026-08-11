"""The cost-component registries — the single authoritative definition (Principle IX).

Two registries live here, and they are *not* the same thing:

- :data:`CHARGE_COMPONENTS` — what the API billed, read straight from a turn's ``usage``
  block. Four token classes whose rates differ by an order of magnitude.
- :data:`ATTRIBUTION_COMPONENTS` — what we *concluded*, the split of those charges across
  context items.

Every plain-language name is defined once, here, and rendered everywhere from this
definition (FR-016). A renderer that re-types a label is a defect.
"""

from dataclasses import dataclass
from typing import Literal

ChargeComponentId = Literal["fresh_input", "cache_write", "cache_read", "output"]
AttributionComponentId = Literal["direct", "carry", "overhead", "output"]

BASIS_VALUES: tuple[str, ...] = ("exact", "measured", "estimated")
CONFIDENCE_VALUES: tuple[str, ...] = ("high", "medium", "low")

# How many significant figures a figure may be displayed to, by confidence (FR-095).
# Internal exactness is never presented as accuracy: a carry figure resting on a splitting
# policy does not get printed to the cent.
#
# `high` is 6 rather than "unlimited" on purpose. A high-confidence figure comes from token
# counts recorded in the transcript, so it IS exact given the rates — the uncertainty left in
# it is a systematic scale error in the price, which the "API-equivalent estimate" label and
# the paired share already carry. Hiding digits would not express that uncertainty, it would
# just lose information. Six figures shows cents for any session total under $10,000 and
# degrades gracefully above it (the renderer never prints finer than a cent regardless).
SIG_FIGS_BY_CONFIDENCE: dict[str, int] = {"high": 6, "medium": 2, "low": 1}


@dataclass(frozen=True)
class CostComponent:
    """One way cost is incurred, with the plain-language name a non-expert reads correctly."""

    id: str
    technical_name: str
    plain_name: str
    description: str


CHARGE_COMPONENTS: tuple[CostComponent, ...] = (
    CostComponent(
        id="fresh_input",
        technical_name="input_tokens",
        plain_name="Your new typing",
        description=(
            "Charged at full rate for content the model had not already stored. This is the "
            "uncached remainder of the prompt, not the size of the conversation."
        ),
    ),
    CostComponent(
        id="cache_write",
        technical_name="cache_creation_input_tokens",
        plain_name="Loading into context",
        description=(
            "The one-time charge for putting content into the conversation, at 1.25x the base "
            "rate for a 5-minute reuse window and 2x for a 1-hour one."
        ),
    ),
    CostComponent(
        id="cache_read",
        technical_name="cache_read_input_tokens",
        plain_name="Keeping context loaded",
        description=(
            "Charged every turn, at a tenth of the base rate, for re-showing everything already "
            "in the conversation."
        ),
    ),
    CostComponent(
        id="output",
        technical_name="output_tokens",
        plain_name="What Claude wrote back",
        description="Charged for everything the model generated, including its thinking.",
    ),
)

ATTRIBUTION_COMPONENTS: tuple[CostComponent, ...] = (
    CostComponent(
        id="direct",
        technical_name="direct",
        plain_name="Loading into context",
        description="The one-time charge for this item entering the conversation.",
    ),
    CostComponent(
        id="carry",
        technical_name="carry",
        plain_name="Keeping context loaded",
        description="The recurring charge for every turn this item stayed available.",
    ),
    CostComponent(
        id="overhead",
        technical_name="overhead",
        plain_name="The conversation itself",
        description=(
            "Cost belonging to the exchange rather than to any one item — the prompts, the "
            "replies, and the scaffolding around them."
        ),
    ),
    CostComponent(
        id="output",
        technical_name="output",
        plain_name="What Claude wrote back",
        description="What the model generated in response. Never charged to a file (FR-005).",
    ),
)

_CHARGE_BY_ID: dict[str, CostComponent] = {c.id: c for c in CHARGE_COMPONENTS}
_ATTRIBUTION_BY_ID: dict[str, CostComponent] = {c.id: c for c in ATTRIBUTION_COMPONENTS}


def charge_component(component_id: str) -> CostComponent:
    """Look up a charge component, raising on an unknown id (Principle I)."""
    try:
        return _CHARGE_BY_ID[component_id]
    except KeyError:
        raise KeyError(
            f"unknown charge component {component_id!r}; "
            f"known: {sorted(_CHARGE_BY_ID)}. Add it to config/components.py, not at the call site."
        ) from None


def attribution_component(component_id: str) -> CostComponent:
    """Look up an attribution component, raising on an unknown id (Principle I)."""
    try:
        return _ATTRIBUTION_BY_ID[component_id]
    except KeyError:
        raise KeyError(
            f"unknown attribution component {component_id!r}; known: {sorted(_ATTRIBUTION_BY_ID)}."
        ) from None


def sig_figs_for(confidence: str) -> int:
    """Significant figures a figure of this confidence may be displayed to (FR-095)."""
    try:
        return SIG_FIGS_BY_CONFIDENCE[confidence]
    except KeyError:
        raise KeyError(
            f"unknown confidence {confidence!r}; known: {sorted(SIG_FIGS_BY_CONFIDENCE)}"
        ) from None
