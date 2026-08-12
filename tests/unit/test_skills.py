"""Contract on splitting the skill catalogue.

"Skills: $67" is not a figure anyone can act on. The catalogue is injected as one block that
lists many skills, and a skill's share of it is exactly its share of the listing text — so the
breakdown is measured rather than modelled, and it sums back to the block.

The rule these tests exist to hold is what the split may *claim*. The listing names a plugin's
skill `plugin:skill`, so plugin origin is observed. It says nothing about the rest, which may be
the project's, the user's own, or bundled with Claude Code — so nothing here calls them "yours".
"""

import pytest

from ccaudit.ingest.skills import (
    ORIGIN_PLUGIN,
    ORIGIN_UNSTATED,
    parse_listing,
    split_tokens,
)

LISTING = (
    "- alpha: Does the alpha thing, at some length so it weighs more.\n"
    "- beta: Short one.\n"
    "- some-plugin:gamma: Supplied by a plugin.\n"
)
NAMES = ["alpha", "beta", "some-plugin:gamma"]


class TestParsingTheListing:
    def test_every_named_skill_becomes_an_entry(self) -> None:
        assert [entry.name for entry in parse_listing(LISTING, NAMES)] == NAMES

    def test_the_name_comes_from_the_record_not_the_prose(self) -> None:
        """A description routinely contains ": ", so reading the name off the text swallowed
        the description into it — "dataviz" became a four-line name and its origin was
        misread. The record lists the names separately and authoritatively."""
        listing = "- dataviz: Use this: whenever you draw a chart: really.\n"
        assert [entry.name for entry in parse_listing(listing, ["dataviz"])] == ["dataviz"]

    def test_a_longer_entry_weighs_more(self) -> None:
        entries = {entry.name: entry.characters for entry in parse_listing(LISTING, NAMES)}
        assert entries["alpha"] > entries["beta"]

    def test_an_unlocatable_entry_falls_back_to_an_even_split_and_says_so(self) -> None:
        """A partial measurement would give the found skills a real size and the rest a
        fabricated one, which is worse than an even split that admits what it is."""
        entries = parse_listing("nothing parseable here", NAMES)
        assert [entry.name for entry in entries] == NAMES
        assert not any(entry.measured for entry in entries)

    def test_no_names_means_no_entries(self) -> None:
        assert parse_listing(LISTING, []) == []


class TestOrigin:
    def test_a_plugin_skill_is_named_by_its_plugin(self) -> None:
        gamma = next(e for e in parse_listing(LISTING, NAMES) if e.name.endswith("gamma"))
        assert gamma.origin == ORIGIN_PLUGIN
        assert gamma.plugin == "some-plugin"

    def test_a_bare_name_is_not_claimed_as_the_users_own(self) -> None:
        """It may be the project's, the user's, or bundled with Claude Code. The listing does
        not distinguish them, so neither may this."""
        alpha = next(e for e in parse_listing(LISTING, NAMES) if e.name == "alpha")
        assert alpha.origin == ORIGIN_UNSTATED
        assert alpha.plugin is None


class TestTheSplitConserves:
    @pytest.mark.parametrize("total", [1_000_000, 7, 0, 1])
    def test_the_parts_sum_to_the_whole(self, total: int) -> None:
        """Invariant A1 reaches down here: a proportional split that dropped its remainder
        would leave the per-skill rows summing to less than the block they came from."""
        entries = parse_listing(LISTING, NAMES)
        assert sum(split_tokens(entries, total)) == total

    def test_no_entries_splits_to_nothing(self) -> None:
        assert split_tokens([], 100) == []
