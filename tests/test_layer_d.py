"""Tests for Layer D — the no-rule fallback (task #23).

Layer D is what ``rule="heuristic"`` invokes and what ``rule="auto"`` falls
to when no rule matches. Its contract is **preserve data, normalize
nothing structural**: no row is dropped and no cell is coerced. Since #35
its output is the canonical *nested* form — every axis becomes a
``{code, label}`` object (time adds ``normalized`` / ``granularity``), and
the observation cell becomes ``{value, unit}`` — the same shape the v2
paths emit, so a caller sees one structure regardless of which path ran.

The point of #23 is that the axis classifier (not a hand-written rule)
decides which axis is ``time``, so best-effort normalization works even on
an uncovered table with a non-conventional axis id.
"""
from __future__ import annotations

from typing import Any

from pyestat._endpoint import ClassObj
from pyestat._engine.apply import apply_rule


def _axis(axis_id: str, name: str, *members: tuple[str, str]) -> ClassObj:
    return ClassObj(
        id=axis_id,
        name=name,
        classes=tuple({"code": c, "name": n, "level": "1"} for c, n in members),
    )


def _layer_d(
    values: tuple[dict[str, Any], ...], class_objs: list[ClassObj]
) -> tuple[dict[str, Any], ...]:
    return apply_rule(values, class_objs, "X", "heuristic")


class TestBestEffortTime:
    def test_parseable_time_axis_is_normalized(self) -> None:
        class_objs = [
            _axis("time", "時間軸（年次）", ("2020000000", "2020年")),
            _axis("cat01", "区分", ("000", "男女計")),
        ]
        out = _layer_d(({"time": "2020000000", "cat01": "000", "value": "126146"},), class_objs)
        time = out[0]["time"]
        assert time["normalized"] == "2020"
        assert time["granularity"] == "yearly"
        assert time["code"] == "2020000000"  # raw preserved
        assert time["label"] == "2020年"

    def test_fiscal_year_code_is_the_april_start_span(self) -> None:
        # A 10-digit fiscal-year code (GDP 0003364993, 「1995年度」) is the
        # April-start year span (#33): yearly granularity for rollups, with
        # normalized "1995-04" so it never merges with calendar 「1995年」 —
        # CPI ships both as siblings in one time axis.
        class_objs = [_axis("time", "時間軸（年度）", ("1995100000", "1995年度"))]
        out = _layer_d(({"time": "1995100000", "value": "5"},), class_objs)
        time = out[0]["time"]
        assert time["normalized"] == "1995-04"
        assert time["granularity"] == "yearly"

    def test_unparseable_time_code_is_left_raw(self) -> None:
        # A code no parser accepts: best-effort leaves it untouched
        # (normalized == code, granularity None) rather than erroring —
        # the object stays whole.
        class_objs = [_axis("time", "時間軸（年度）", ("1995300000", "1995年度?"))]
        out = _layer_d(({"time": "1995300000", "value": "5"},), class_objs)
        time = out[0]["time"]
        assert time["normalized"] == "1995300000"
        assert time["granularity"] is None

    def test_year_span_member_name_demotes_monthly_to_yearly(self) -> None:
        # Population vital statistics (0003001309): the code 2006001010 is
        # byte-identical to monthly 2006-10, but the member name
        # 「2006年10月～2007年9月」 shows an October-start annual aggregate.
        # Misreading it as monthly puts a year's deaths in one month (#33).
        class_objs = [
            _axis("time", "時間軸（年間）", ("2006001010", "2006年10月～2007年9月")),
        ]
        out = _layer_d(({"time": "2006001010", "value": "1084450"},), class_objs)
        time = out[0]["time"]
        assert time["granularity"] == "yearly"
        assert time["normalized"] == "2006-10"  # start month, per #33 contract
        assert time["label"] == "2006年10月～2007年9月"

    def test_time_detected_on_non_conventional_axis_id(self) -> None:
        # axis_id is cat03, but the classifier still reads it as `time`
        # (name 時間軸 + date-shape codes), so Layer D normalizes it. This
        # is the classifier doing the work a hand-written rule would.
        class_objs = [_axis("cat03", "時間軸（年次）", ("2020000000", "2020年"))]
        out = _layer_d(({"cat03": "2020000000", "value": "5"},), class_objs)
        cat03 = out[0]["cat03"]
        assert cat03["normalized"] == "2020"
        assert cat03["code"] == "2020000000"
        assert cat03["granularity"] == "yearly"


class TestDataPreservation:
    def test_axis_becomes_code_label_object(self) -> None:
        class_objs = [
            _axis("time", "時間軸（年次）", ("2020000000", "2020年")),
            _axis("cat01", "区分", ("000", "男女計")),
        ]
        out = _layer_d(({"time": "2020000000", "cat01": "000", "value": "1"},), class_objs)
        assert out[0]["cat01"] == {"code": "000", "label": "男女計"}

    def test_observation_cell_becomes_value_unit_object(self) -> None:
        # The number and its unit are folded into one self-describing cell.
        class_objs = [_axis("cat01", "区分", ("000", "男女計"))]
        out = _layer_d(({"cat01": "000", "value": "1097352", "unit": "人"},), class_objs)
        assert out[0]["value"] == {"value": "1097352", "unit": "人"}

    def test_marker_and_string_cells_pass_through(self) -> None:
        # Suppression markers and genuine unit strings are preserved as-is
        # — Layer D never coerces the cell value, only wraps it with its unit.
        class_objs = [
            _axis("cat02", "区分", ("100", "単位")),
            _axis("time", "時間軸（年次）", ("2020000000", "2020年")),
        ]
        rows = (
            {"cat02": "100", "time": "2020000000", "value": "-"},
            {"cat02": "100", "time": "2020000000", "value": "ＮＯ"},
        )
        out = _layer_d(rows, class_objs)
        assert out[0]["value"]["value"] == "-"
        assert out[1]["value"]["value"] == "ＮＯ"

    def test_no_time_axis_returns_labelled_rows_without_error(self) -> None:
        class_objs = [_axis("cat01", "区分", ("000", "男女計"))]
        out = _layer_d(({"cat01": "000", "value": "1"},), class_objs)
        assert out[0]["cat01"] == {"code": "000", "label": "男女計"}
        assert "time" not in out[0]
