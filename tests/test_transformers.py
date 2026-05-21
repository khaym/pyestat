"""Tests for the Layer-3 Transformer pipeline (TimeNormalizer, ValueCaster).

Each Transformer is a stream-in / stream-out generator. The trade
table has 3.8M rows; if any Transformer materializes the stream the
runtime cost goes from negligible to "kill the process". The
generator-shape tests guard against that regression specifically.
"""
from __future__ import annotations

import pytest

from pyestat._rule import Rule
from pyestat._registry import RegistryKeyError
from pyestat._transformers import TimeNormalizer, TransformContext, ValueCaster


def _rule(*, format: str = "monthly_e_stat", value_type: str = "number") -> Rule:
    return Rule.model_validate(
        {
            "schema_version": "1",
            "match": {"statsCode": "x"},
            "axes": {"time": {"id": "time", "format": format}},
            "value": {"type": value_type},
        }
    )


def _ctx() -> TransformContext:
    return TransformContext(stats_data_id="X", class_inf={}, axes_meta={})


class TestTimeNormalizer:
    def test_replaces_axis_value_with_normalized_and_preserves_raw(self) -> None:
        # DESIGN.md commits to a three-field output: time (normalized),
        # time_code (raw), time_granularity (tag). Pinning all three
        # so a future "simplification" cannot drop one silently.
        rows = iter([{"time": "2022000101", "value": "42"}])
        out = list(TimeNormalizer().transform(rows, _rule(), _ctx()))
        assert out == [
            {"time": "2022-01", "time_code": "2022000101", "time_granularity": "monthly", "value": "42"},
        ]

    def test_skips_rows_missing_the_time_axis(self) -> None:
        # Some response rows lack the time axis (aggregation rows on a
        # different axis). The normalizer must pass them through
        # untouched rather than crash on a ``None`` code.
        rows = iter([{"value": "1"}, {"time": "2022000202", "value": "2"}])
        out = list(TimeNormalizer().transform(rows, _rule(), _ctx()))
        assert out[0] == {"value": "1"}  # untouched
        assert out[1]["time"] == "2022-02"

    def test_unknown_format_raises_with_registry_error(self) -> None:
        # Format name resolution is deferred from rule-load to first
        # transform call (so a rule referencing a parser added in a
        # newer pyestat still loads on an old library). The error
        # surfaces only when the row stream actually needs the parser.
        rows = iter([{"time": "2022000101", "value": "1"}])
        with pytest.raises(RegistryKeyError):
            list(TimeNormalizer().transform(rows, _rule(format="not_a_parser"), _ctx()))

    def test_is_lazy(self) -> None:
        # Lazy evaluation is the only thing that lets a 3.8M-row table
        # flow through the pipeline without OOM. If a future change
        # accidentally turns the generator into a list comprehension,
        # constructing a fresh transformer on an infinite source must
        # not exhaust memory — this test asserts only the first row is
        # consumed before the caller pulls more.
        consumed = 0

        def source():
            nonlocal consumed
            while True:
                consumed += 1
                yield {"time": "2022000101", "value": str(consumed)}

        gen = TimeNormalizer().transform(source(), _rule(), _ctx())
        next(gen)
        assert consumed == 1
        next(gen)
        assert consumed == 2


class TestValueCaster:
    def test_number_casts_integers_to_int(self) -> None:
        # e-Stat returns all numbers as strings; the caster must produce
        # an int when the value has no fractional part so downstream
        # analytics don't see "1.0" where "1" was meant.
        out = list(ValueCaster().transform(iter([{"value": "126146"}]), _rule(), _ctx()))
        assert out == [{"value": 126146}]
        assert isinstance(out[0]["value"], int)

    def test_number_casts_decimals_to_float(self) -> None:
        out = list(ValueCaster().transform(iter([{"value": "3.14"}]), _rule(), _ctx()))
        assert out == [{"value": pytest.approx(3.14)}]

    def test_number_leaves_marker_strings_untouched(self) -> None:
        # e-Stat encodes "no data" as "-" and confidential cells as
        # "***" inside the value field; coercing those to float would
        # raise. The caster must preserve them as strings so analysts
        # can distinguish "missing" from "zero".
        rows = [{"value": "-"}, {"value": "***"}, {"value": ""}]
        out = list(ValueCaster().transform(iter(rows), _rule(), _ctx()))
        assert out == rows

    def test_string_stringifies(self) -> None:
        # Useful when the value column is a unit symbol (the trade table
        # uses "ＮＯ" / "ＫＧ" for some cat02 codes); the type signal
        # tells callers to expect a string everywhere.
        out = list(ValueCaster().transform(iter([{"value": 42}]), _rule(value_type="string"), _ctx()))
        assert out == [{"value": "42"}]
