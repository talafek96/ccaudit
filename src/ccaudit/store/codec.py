"""A generic dataclass <-> JSON codec, driven by type hints and compiled once per type.

**Why generic rather than one serialiser per type.** Ten hand-written serialisers are ten
places to forget a field, and a forgotten field is a figure that silently changes when it comes
back out of the store. This codec reads :func:`dataclasses.fields` and the resolved type hints,
so a field added upstream is carried automatically and a field it cannot represent raises at
the point of the attempt rather than being dropped.

**Why this is safe where a re-derivation would not be.** The store must hold a *record of the
conclusion*, never the material to recompute it (FR-105). The difference is checkability:
``decode(encode(x)) == x`` is a property that can be asserted directly, over real sessions,
which is what ``tests/unit/test_codec.py`` and ``tests/system/test_cache.py`` do. A second
derivation offers no equivalent — you can only compare outputs and hope you tried the case that
differs.

**Why it compiles.** The obvious implementation walks the type hint on every value and decides
what to do. Measured against a real corpus, that was *slower than recomputing the analysis*:
600,000 calls to ``get_origin``/``get_args``/``is_dataclass``, which is a cache costing more
than the thing it caches. So a type is inspected once and turned into a closure that already
knows what to do, and the hot path only calls closures. Same behaviour, none of the reflection.

Deliberately narrow. It handles precisely the shapes the analysis model uses today —
``str``/``int``/``bool``/``None``, ``list[T]``, ``tuple[T, ...]``, ``set[T]``, ``dict[str, T]``,
and nested dataclasses — and **raises on anything else**. A silent fallback (``str(value)``, or
``pickle``) is how a float sneaks into a money field, so there is none.

``float`` is rejected on purpose, not by omission: money is integer micro-dollars everywhere in
this project, and a float surviving a JSON round trip is not guaranteed to be the same number.
"""

import dataclasses
import types
import typing
from collections.abc import Callable
from functools import cache
from typing import Any

# Values that survive JSON unchanged. `float` is absent deliberately — see the module docstring.
_SCALARS: tuple[type, ...] = (str, int, bool, type(None))

# How a container is tagged on the way out, so the way back in is not a guess. A bare JSON list
# cannot say whether it was a list, a tuple, or a set, and restoring the wrong one breaks
# equality — which is the one property this module exists to preserve.
_TUPLE_TAG = "__tuple__"
_SET_TAG = "__set__"


class UnsupportedTypeError(TypeError):
    """A field whose type this codec cannot represent faithfully.

    Fatal by design (Principle I). The alternative — coercing it to a string and moving on —
    would put a value in the store that does not restore to what went in, and the caller would
    have no way to know.
    """


def encode(value: Any, hint: Any) -> Any:
    """Convert ``value`` (of declared type ``hint``) into JSON-safe data."""
    return _encoder(hint)(value)


def decode[T](data: Any, hint: type[T]) -> T:
    """Rebuild a value of declared type ``hint`` from data produced by :func:`encode`."""
    return typing.cast(T, _decoder(typing.cast(Any, hint))(data))


def _optional_inner(args: tuple[Any, ...], hint: Any) -> Any:
    """The ``X`` of an ``X | None``. Anything wider has no discriminator to restore it by."""
    inner = [arg for arg in args if arg is not type(None)]
    if len(inner) != 1:
        raise UnsupportedTypeError(
            f"cannot represent union {hint!r}: only 'X | None' is supported, because a wider "
            f"union cannot be restored without a discriminator"
        )
    return inner[0]


@cache
def _encoder(hint: Any) -> Callable[[Any], Any]:
    """A function that encodes any value of type ``hint``. Built once, then called per value."""
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    if origin in (typing.Union, types.UnionType):
        inner = _encoder(_optional_inner(args, hint))
        return lambda value: None if value is None else inner(value)

    if origin is tuple:
        if len(args) != 2 or args[1] is not Ellipsis:
            raise UnsupportedTypeError(
                f"cannot encode {hint!r}: only homogeneous 'tuple[T, ...]' is supported"
            )
        item = _encoder(args[0])
        return lambda value: {_TUPLE_TAG: [entry_value for entry_value in map(item, value)]}

    if origin in (set, frozenset):
        item = _encoder(args[0])
        # Sorted so the stored bytes are the same on every run and machine (FR-017).
        return lambda value: {_SET_TAG: sorted(map(item, value))}

    if origin is list:
        item = _encoder(args[0])
        return lambda value: list(map(item, value))

    if origin is dict:
        key_type, value_type = args
        if key_type is not str:
            raise UnsupportedTypeError(
                f"cannot encode {hint!r}: JSON object keys are strings, and coercing "
                f"{key_type!r} would not restore to the same key"
            )
        item = _encoder(value_type)
        return lambda value: {key: item(entry) for key, entry in value.items()}

    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        shape = [(name, _encoder(field_type)) for name, field_type in _fields(hint)]
        return lambda value: {name: fn(getattr(value, name)) for name, fn in shape}

    if hint is float:
        raise UnsupportedTypeError(
            "refusing to encode a float: money here is integer micro-dollars, and a float is "
            "not guaranteed to restore to the same value"
        )

    if hint in _SCALARS or hint is Any:
        return _scalar

    raise UnsupportedTypeError(f"no encoding for type {hint!r}")


def _scalar(value: Any) -> Any:
    if not isinstance(value, _SCALARS):
        raise UnsupportedTypeError(f"value {value!r} is not JSON-safe")
    return value


@cache
def _decoder(hint: Any) -> Callable[[Any], Any]:
    """A function that decodes any value of type ``hint``. Built once, then called per value."""
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    if origin in (typing.Union, types.UnionType):
        inner = _decoder(_optional_inner(args, hint))
        return lambda data: None if data is None else inner(data)

    if origin is tuple:
        item = _decoder(args[0])
        return lambda data: tuple(map(item, data[_TUPLE_TAG]))

    if origin in (set, frozenset):
        item = _decoder(args[0])
        builder = typing.cast(Any, origin)
        return lambda data: builder(map(item, data[_SET_TAG]))

    if origin is list:
        item = _decoder(args[0])
        return lambda data: list(map(item, data))

    if origin is dict:
        item = _decoder(args[1])
        return lambda data: {key: item(entry) for key, entry in data.items()}

    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        shape = [(name, _decoder(field_type)) for name, field_type in _fields(hint)]
        builder = typing.cast(Any, hint)
        names = [name for name, _ in shape]

        def build(data: Any) -> Any:
            missing = [name for name in names if name not in data]
            if missing:
                # A field added since this row was written. Restoring it from the default would
                # hand back something that is not what was stored, with nothing to signal the
                # difference — so the row is refused and the caller recomputes, which costs
                # time and cannot cost correctness.
                raise UnsupportedTypeError(
                    f"stored {builder.__name__} is missing field(s) {missing}: it was written "
                    f"by a different version of this model and cannot be restored faithfully"
                )
            return builder(**{name: fn(data[name]) for name, fn in shape})

        return build

    if hint is float:
        raise UnsupportedTypeError("refusing to decode a float; see encode")

    if hint in _SCALARS or hint is Any:
        return _identity

    raise UnsupportedTypeError(f"no decoding for type {hint!r}")


def _identity(data: Any) -> Any:
    return data


@cache
def _fields(cls: type) -> tuple[tuple[str, Any], ...]:
    """A dataclass's ``(field name, resolved type)`` pairs, resolved once per class."""
    hints = typing.get_type_hints(cls)
    return tuple((field.name, hints[field.name]) for field in dataclasses.fields(cls))
