"""Tests for Layer D — the no-rule fallback (task #23).

Layer D is what ``rule="heuristic"`` invokes and what ``rule="auto"`` falls
to when no rule matches. Its contract is **preserve data, normalize
nothing structural**: raw axis-id codes stay, every axis gains a
``{axis}_label``, and the axis the classifier calls ``time`` gets a
best-effort normalization (if a parser recognises the code). A parse miss
is silent — Layer D never raises and never drops a row.

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
        row = out[0]
        assert row["time"] == "2020"
        assert row["time_granularity"] == "yearly"
        assert row["time_code"] == "2020000000"  # raw preserved

    def test_unparseable_time_code_is_left_raw(self) -> None:
        # A 10-digit fiscal-year code (1995100000) no built-in parser
        # accepts: best-effort leaves it untouched rather than erroring.
        class_objs = [_axis("time", "時間軸（年度）", ("1995100000", "1995年度"))]
        out = _layer_d(({"time": "1995100000", "value": "5"},), class_objs)
        row = out[0]
        assert row["time"] == "1995100000"
        assert "time_granularity" not in row

    def test_time_detected_on_non_conventional_axis_id(self) -> None:
        # axis_id is cat03, but the classifier still reads it as `time`
        # (name 時間軸 + date-shape codes), so Layer D normalizes it. This
        # is the classifier doing the work a hand-written rule would.
        class_objs = [_axis("cat03", "時間軸（年次）", ("2020000000", "2020年"))]
        out = _layer_d(({"cat03": "2020000000", "value": "5"},), class_objs)
        row = out[0]
        assert row["cat03"] == "2020"
        assert row["cat03_code"] == "2020000000"
        assert row["time_granularity"] == "yearly"


class TestDataPreservation:
    def test_labels_added_and_raw_codes_kept(self) -> None:
        class_objs = [
            _axis("time", "時間軸（年次）", ("2020000000", "2020年")),
            _axis("cat01", "区分", ("000", "男女計")),
        ]
        out = _layer_d(({"time": "2020000000", "cat01": "000", "value": "1"},), class_objs)
        row = out[0]
        assert row["cat01"] == "000"  # raw code untouched
        assert row["cat01_label"] == "男女計"

    def test_marker_and_string_cells_pass_through(self) -> None:
        # Suppression markers and genuine unit strings are preserved as-is
        # — Layer D never coerces the cell value.
        class_objs = [
            _axis("cat02", "区分", ("100", "単位")),
            _axis("time", "時間軸（年次）", ("2020000000", "2020年")),
        ]
        rows = (
            {"cat02": "100", "time": "2020000000", "value": "-"},
            {"cat02": "100", "time": "2020000000", "value": "ＮＯ"},
        )
        out = _layer_d(rows, class_objs)
        assert out[0]["value"] == "-"
        assert out[1]["value"] == "ＮＯ"

    def test_no_time_axis_returns_labelled_rows_without_error(self) -> None:
        class_objs = [_axis("cat01", "区分", ("000", "男女計"))]
        out = _layer_d(({"cat01": "000", "value": "1"},), class_objs)
        assert out[0]["cat01_label"] == "男女計"
        assert "time_granularity" not in out[0]
