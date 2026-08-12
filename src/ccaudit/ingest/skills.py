"""Splitting the skill listing into the individual skills it lists.

**Why this exists.** The skill catalogue is injected as one blob and was attributed as one
item, so a report could say "Skills: $67" and nothing more. That is the wrong shape for the
question people actually have — *which* skills, and can I do anything about them? Some are
invoked constantly and some never; some are the team's own and some arrive with an installed
plugin and cannot be changed by editing this repo.

**Why the split is measured rather than assumed.** The attachment carries the listing's full
text, and the text is one entry per skill. A skill's share of the resident catalogue is exactly
its share of that text — no model of "importance", no estimate. The per-skill sizes are
allocated from the measured total with the same largest-remainder split money uses, so they sum
to the blob exactly and the breakdown still reconciles (invariant A1).

**Origin is read, not guessed.** Claude Code names a plugin's skill ``plugin:skill``; a bare
name is one the user or the project supplies. That is an observable fact in the record, which
is the only kind this project acts on.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ccaudit.money import allocate

# A plugin's skill is named `plugin:skill`. Nothing else in a listing carries a colon in its
# name, so the separator is the origin.
PLUGIN_SEPARATOR = ":"

ORIGIN_PLUGIN = "plugin"
# Deliberately *not* "yours". A bare name in the listing may be a skill this project defines, one
# the user keeps in their home directory, or one bundled with Claude Code itself — and the
# listing does not distinguish them. Calling all three "yours" would tell a reader they can edit
# something they may not own, which is a claim the record does not support (Principle X).
ORIGIN_UNSTATED = "unstated"


@dataclass(frozen=True)
class ListedSkill:
    """One skill in the catalogue, with the share of it that is this skill's text."""

    name: str
    characters: int
    origin: str
    # Whether ``characters`` is this skill's real share of the listing text. False when the
    # entries could not be located and the split fell back to an even one.
    measured: bool = True

    @property
    def plugin(self) -> str | None:
        """The plugin that supplies it, or ``None`` where the listing does not say."""
        if self.origin != ORIGIN_PLUGIN:
            return None
        return self.name.split(PLUGIN_SEPARATOR, 1)[0]


def origin_of(name: str) -> str:
    return ORIGIN_PLUGIN if PLUGIN_SEPARATOR in name else ORIGIN_UNSTATED


def parse_listing(content: str, names: Sequence[str] | None = None) -> list[ListedSkill]:
    """Split a skill listing into its entries, measured by the characters each occupies.

    The **names are taken from the record, never parsed out of the prose**. A description
    routinely contains ": ", so a regex that reads the name off the text swallows the
    description into it — which it did, turning "dataviz" into a four-line name and
    misreading its origin. The record lists the names separately and authoritatively; the text
    is only used to find where each entry begins.

    Falls back to an even split when the text cannot be located, so a listing whose format
    changes still yields per-skill rows rather than collapsing back to one blob. That fallback
    is a *declared* even split rather than a measured one, and says so through ``measured``.
    """
    names = list(names or [])
    if not names:
        return []
    text = content or ""
    starts: list[tuple[int, str]] = []
    for name in names:
        position = text.find(f"- {name}: ")
        if position >= 0:
            starts.append((position, name))
    if len(starts) != len(names):
        # Some entry could not be located, so no entry's length can be trusted. An even split
        # across the names is honest and is marked as unmeasured; a partial measurement would
        # silently give the located skills a real size and the rest a fabricated one.
        return [
            ListedSkill(name=name, characters=1, origin=origin_of(name), measured=False)
            for name in names
        ]
    starts.sort()
    entries: list[ListedSkill] = []
    for index, (position, name) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        entries.append(
            ListedSkill(name=name, characters=max(1, end - position), origin=origin_of(name))
        )
    return entries


def split_tokens(entries: list[ListedSkill], total_tokens: int) -> list[int]:
    """Divide the listing's measured size across its entries, conserving the total exactly.

    The same largest-remainder allocation money uses: a proportional split that dropped its
    remainder would leave the per-skill rows summing to less than the blob they came from.
    """
    if not entries:
        return []
    return allocate(total_tokens, [entry.characters for entry in entries])
