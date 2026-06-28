"""Tests for the canonical output contract and its flat projection.

``canonical`` is the single home for "what one converted row looks like".
Every conversion path (Layer D, v2 1:1, v2 pivot) builds its cells through
the constructors here, so the nested output shape cannot drift between
paths. The nested form is canonical; :func:`to_flat_rows` is a cheap,
lossless projection back to one column per field for callers who prefer
the flat suffix convention (and pandas).

Business rules pinned here:

* A dimension cell is ``{code, label}``; a time cell adds ``normalized`` /
  ``granularity``; a measure cell is ``{value, unit}``.
* ``time_cell`` is *total* — an unrecognised or non-string code keeps
  ``normalized == code`` and ``granularity is None`` rather than raising.
* Flattening reproduces the legacy suffix convention (``cat01`` /
  ``cat01_label``; ``time`` / ``time_code`` / ``time_label`` /
  ``time_granularity``) and leaves an already-flat (raw) row untouched.
"""
from __future__ import annotations

import pytest

from pyestat._engine.canonical import dimension, measure, time_cell, to_flat_rows


class TestConstructors:
    def test_dimension_is_code_and_label(self) -> None:
        assert dimension("01000", "総数") == {"code": "01000", "label": "総数"}

    def test_measure_is_value_and_unit(self) -> None:
        assert measure("1097352", "人") == {"value": "1097352", "unit": "人"}

    def test_time_cell_normalizes_and_keeps_all_four_fields(self) -> None:
        # The Done shape: raw code, e-Stat display label, ISO-leaning
        # normalized value, and the granularity tag, all in one object.
        # The label's range marker flags an October-start annual aggregate
        #, so the granularity is yearly despite the monthly code shape.
        assert time_cell("2006001010", "2006年10月～2007年9月") == {
            "code": "2006001010",
            "label": "2006年10月～2007年9月",
            "normalized": "2006-10",
            "granularity": "yearly",
        }


class TestTimeCellIsTotal:
    def test_unrecognised_code_keeps_normalized_equal_to_code(self) -> None:
        # A code no built-in parser accepts: the cell is still well-formed
        # (normalized == code, granularity None) so the shape is stable and
        # no path raises. (The fiscal shape 1995100000 parses as a plain
        # year, so an unclaimed separator stands in here.)
        cell = time_cell("1995300000", "1995年度?")
        assert cell["normalized"] == "1995300000"
        assert cell["granularity"] is None

    def test_non_string_code_does_not_raise(self) -> None:
        # An int year a JSON/YAML layer coerced must degrade, not crash the
        # regex parsers.
        cell = time_cell(2020, "2020")
        assert cell["normalized"] == 2020
        assert cell["granularity"] is None


class TestFlatProjection:
    def test_dimension_expands_to_code_and_label_suffix(self) -> None:
        out = to_flat_rows(({"cat01": dimension("01000", "総数")},))
        assert out[0] == {"cat01": "01000", "cat01_label": "総数"}

    def test_conventional_time_axis_matches_legacy_flat_keys(self) -> None:
        # The `time` axis flattens to the historical Layer D shape so
        # downstream code written against it keeps working.
        out = to_flat_rows(({"time": time_cell("2006001010", "2006年10月～2007年9月")},))
        assert out[0] == {
            "time": "2006-10",
            "time_code": "2006001010",
            "time_label": "2006年10月～2007年9月",
            "time_granularity": "yearly",  # range-marked label = year span
        }

    def test_non_conventional_time_axis_suffixes_granularity_by_key(self) -> None:
        # When the classifier reads a non-`time` axis as time (e.g. cat03
        # named 時間軸), the granularity flattens under that key, not a fixed
        # `time_granularity` — so the flat columns stay self-consistent.
        out = to_flat_rows(({"cat03": time_cell("2020000000", "2020年")},))
        assert out[0] == {
            "cat03": "2020",
            "cat03_code": "2020000000",
            "cat03_label": "2020年",
            "cat03_granularity": "yearly",
        }

    def test_value_measure_flattens_to_value_and_unit_siblings(self) -> None:
        out = to_flat_rows(({"value": measure("1097352", "人")},))
        assert out[0] == {"value": "1097352", "unit": "人"}

    def test_named_measure_column_suffixes_unit_by_key(self) -> None:
        # A pivot measure column (trade's 数量 / 金額) keeps its unit under a
        # per-column key so two measures with different units don't collide.
        out = to_flat_rows((
            {"数量": measure("16", "ＮＯ"), "金額": measure("35220", "千円")},
        ))
        assert out[0] == {
            "数量": "16", "数量_unit": "ＮＯ",
            "金額": "35220", "金額_unit": "千円",
        }

    def test_missing_pivot_measure_stays_none(self) -> None:
        # A dropped meta member surfaces as None (the pivot contract); flatten
        # leaves it as a single None column, not an exploded pair.
        out = to_flat_rows(({"unit": None, "quantity": measure("7", "ＮＯ")},))
        assert out[0] == {"unit": None, "quantity": "7", "quantity_unit": "ＮＯ"}

    def test_scalar_cells_pass_through_unchanged(self) -> None:
        # A raw (rule=None) row is all scalars: flattening is a no-op, so
        # `.to_flat()` is safe to call on any response shape.
        raw = ({"cat01": "11", "time": "1996000103", "unit": "10億円", "value": "57123.8"},)
        assert to_flat_rows(raw) == raw

    def test_colliding_flat_keys_raise_instead_of_silently_overwriting(self) -> None:
        # A "value" measure flattens its unit to the bare `unit` key; a sibling
        # column literally named "unit" also writes `unit`. The rule's own
        # unique-column check cannot see these derived keys, so flatten guards
        # the collision loudly rather than dropping one field by dict order.
        rows = ({"value": measure("1", "人"), "unit": measure("x", None)},)
        with pytest.raises(ValueError, match="collision"):
            to_flat_rows(rows)

    def test_flatten_is_idempotent(self) -> None:
        # Flattening an already-flat row yields the same row — the projection
        # only fires on canonical objects, never on its own output.
        once = to_flat_rows(({"cat01": dimension("01000", "総数"),
                              "value": measure("1", "人")},))
        assert to_flat_rows(once) == once
