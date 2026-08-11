"""Contract on the dataclass codec.

One property matters and everything here serves it: ``decode(encode(x)) == x``. That equality
is the entire reason the store is allowed to exist — it is what makes a cached figure the same
figure rather than a second derivation of it (FR-105). A codec that silently drops a field, or
restores a tuple as a list, breaks it quietly, so the cases below are the ways that could
happen.
"""

from dataclasses import dataclass, field

import pytest

from ccaudit.store.codec import UnsupportedTypeError, decode, encode


@dataclass(frozen=True)
class Leaf:
    name: str
    count: int
    flag: bool
    maybe: str | None


@dataclass
class Branch:
    leaf: Leaf
    leaves: list[Leaf]
    names: tuple[str, ...]
    tags: set[str]
    index: dict[str, Leaf]
    empty: list[Leaf] = field(default_factory=list)


def sample() -> Branch:
    leaf = Leaf(name="a", count=1, flag=True, maybe=None)
    other = Leaf(name="b", count=-2, flag=False, maybe="set")
    return Branch(
        leaf=leaf,
        leaves=[leaf, other],
        names=("x", "y"),
        tags={"beta", "alpha"},
        index={"one": leaf, "two": other},
    )


class TestRoundTrip:
    def test_a_nested_structure_survives_intact(self) -> None:
        value = sample()
        assert decode(encode(value, Branch), Branch) == value

    def test_a_tuple_does_not_come_back_as_a_list(self) -> None:
        """Equality is the property; ``('x',) != ['x']`` would break it silently."""
        restored = decode(encode(sample(), Branch), Branch)
        assert isinstance(restored.names, tuple)

    def test_a_set_does_not_come_back_as_a_list(self) -> None:
        restored = decode(encode(sample(), Branch), Branch)
        assert isinstance(restored.tags, set)

    def test_none_survives_as_none_not_as_a_string(self) -> None:
        restored = decode(encode(sample(), Branch), Branch)
        assert restored.leaf.maybe is None

    def test_an_empty_collection_survives(self) -> None:
        assert decode(encode(sample(), Branch), Branch).empty == []

    def test_encoding_is_stable_across_runs(self) -> None:
        """The stored bytes must not depend on set iteration order (FR-017)."""
        assert encode(sample(), Branch) == encode(sample(), Branch)

    def test_a_set_is_sorted_on_the_way_out(self) -> None:
        assert encode(sample(), Branch)["tags"]["__set__"] == ["alpha", "beta"]


class TestItRefusesRatherThanGuesses:
    def test_a_float_is_refused(self) -> None:
        """Money is integer micro-dollars; a float is not guaranteed to survive JSON."""

        @dataclass
        class HasFloat:
            rate: float

        with pytest.raises(UnsupportedTypeError, match="float"):
            encode(HasFloat(rate=1.5), HasFloat)

    def test_a_non_string_dict_key_is_refused(self) -> None:
        @dataclass
        class IntKeys:
            by_turn: dict[int, str]

        with pytest.raises(UnsupportedTypeError, match="keys are strings"):
            encode(IntKeys(by_turn={1: "a"}), IntKeys)

    def test_a_heterogeneous_tuple_is_refused(self) -> None:
        @dataclass
        class Pair:
            both: tuple[str, int]

        with pytest.raises(UnsupportedTypeError, match="homogeneous"):
            encode(Pair(both=("a", 1)), Pair)

    def test_a_wide_union_is_refused(self) -> None:
        """Restoring it would need a discriminator, and inventing one is speculative."""

        @dataclass
        class Wide:
            either: str | int

        with pytest.raises(UnsupportedTypeError, match="union"):
            encode(Wide(either="a"), Wide)

    def test_an_unknown_type_is_refused_rather_than_stringified(self) -> None:
        @dataclass
        class HasPath:
            where: complex

        with pytest.raises(UnsupportedTypeError):
            encode(HasPath(where=1j), HasPath)


class TestStoredDataFromAnotherShape:
    def test_a_missing_field_is_refused_rather_than_defaulted(self) -> None:
        """A row written before a field existed cannot be restored faithfully.

        Filling it from the default would hand back something that is not what was stored,
        with nothing to signal the difference. Refusing costs one recomputation.
        """
        payload = encode(sample(), Branch)
        del payload["leaf"]["maybe"]
        with pytest.raises(UnsupportedTypeError, match="missing field"):
            decode(payload, Branch)

    def test_an_extra_field_is_ignored(self) -> None:
        """A field removed since the row was written says nothing about the ones that remain."""
        payload = encode(sample(), Branch)
        payload["leaf"]["gone"] = "old"
        assert decode(payload, Branch).leaf == sample().leaf
