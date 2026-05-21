"""Tests for the Layer-3 registry abstraction.

Registry is a tiny name-to-impl lookup. Today only TIME_PARSERS uses
it, but the Decision-D expansion list adds STANDARD_CODES (ISO 8601 /
JIS / ISO 3166) later, so the abstraction needs to be exercised in
isolation so a future second instance can be added without surprise.
"""
from __future__ import annotations

import pytest

from pyestat._registry import Registry, RegistryKeyError
from pyestat._time import TIME_PARSERS


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


class TestTimeParsersRegistry:
    def test_three_built_in_parsers_present(self) -> None:
        # DESIGN.md commits to exactly these three at MVP. Pinning the
        # set guards against accidental additions slipping into a rule
        # schema before the schema is bumped.
        assert set(TIME_PARSERS.names()) == {
            "monthly_e_stat",
            "quarterly_e_stat",
            "yearly",
        }

    def test_resolved_parser_actually_parses(self) -> None:
        parser = TIME_PARSERS.resolve("monthly_e_stat")
        result = parser("2022000101")
        assert result.normalized == "2022-01"
        assert result.granularity == "monthly"
