"""Name-to-implementation lookup used by Layer 3.

A trivial wrapper around a ``dict`` — its only job is to centralize
the error message ("unknown 'foo'; known: …") so rule-authoring
typos surface as something a non-engineer can act on. Today only the
time parsers exercise this; the Decision-D expansion list adds a
standard-codes registry later for ISO 8601 / JIS / ISO 3166 mapping.
"""
from __future__ import annotations

from typing import Generic, TypeVar


T = TypeVar("T")


class RegistryKeyError(KeyError):
    """Raised when a rule references a registry entry that is not registered.

    Inherits from ``KeyError`` so callers that ``try / except KeyError``
    around a missing lookup continue to work, but the dedicated subclass
    is the documented contract.
    """


class Registry(Generic[T]):
    """Simple name-keyed registry with informative misses.

    ``kind`` is the human-readable noun used in error messages
    ("time parser", "standard-code mapper"); it costs nothing to set
    and turns a generic ``KeyError`` into something actionable.
    """

    def __init__(self, *, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, impl: T) -> None:
        if name in self._items:
            # Silently overwriting would let an imported plugin shadow
            # a built-in entry at import time and never tell anyone.
            raise ValueError(
                f"{self._kind} {name!r} is already registered"
            )
        self._items[name] = impl

    def resolve(self, name: str) -> T:
        try:
            return self._items[name]
        except KeyError as exc:
            raise RegistryKeyError(
                f"unknown {self._kind} {name!r} "
                f"(known: {sorted(self._items)})"
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._items)
