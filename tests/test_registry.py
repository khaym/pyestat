"""Tests for the Layer-3 registry abstraction.

Registry is a tiny name-to-impl lookup. These tests exercise the
primitive in isolation with throwaway instances, so a future second
instance (e.g. a future STANDARD_CODES) can be added without surprise. The
live production instance — ``role_defaults.TRANSFORMS`` — has its own
content/behavior coverage in ``test_role_defaults.py``.
"""
from __future__ import annotations

import pytest

from pyestat._engine.registry import Registry, RegistryKeyError


class TestRegistry:
    def test_register_then_resolve(self) -> None:
        r: Registry[int] = Registry(kind="thing")
        r.register("a", 1)
        assert r.resolve("a") == 1

    def test_resolve_unknown_lists_known_options(self) -> None:
        # The error message lists known entries because a rule author
        # hitting this almost certainly mistyped a format name — showing
        # the alternatives turns a 30-second debug into a 3-second one.
        r: Registry[int] = Registry(kind="thing")
        r.register("monthly_e_stat", 1)
        with pytest.raises(RegistryKeyError) as exc:
            r.resolve("monthly")
        assert "monthly_e_stat" in str(exc.value)

    def test_double_register_is_rejected(self) -> None:
        # Silent overwrite would let a plugin shadow a built-in parser
        # at import time; making it explicit forces the conflict into
        # the open.
        r: Registry[int] = Registry(kind="thing")
        r.register("a", 1)
        with pytest.raises(ValueError, match="already"):
            r.register("a", 2)

    def test_names_returned_sorted_for_stable_diffs(self) -> None:
        r: Registry[int] = Registry(kind="thing")
        r.register("b", 2)
        r.register("a", 1)
        assert r.names() == ["a", "b"]
