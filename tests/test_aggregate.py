"""Tests for aggregate vs. detail row selection (#36).

These tests encode the business rule a caller relies on: in a table whose
codes form a hierarchy (総数 → 大分類 → 品目, or 全国 → 都道府県), summing a
measure across a mix of total and leaf rows double-counts. Selecting the
leaves (``"exclude"`` the aggregates) yields a single, self-consistent grain
safe to aggregate; selecting the totals (``"only"``) yields the subtotals.

The detection is deterministic and **per-response**: a code is an aggregate
iff another member in *this* table names it as its ``@parentCode`` (it has
children here), never by an absolute hierarchy. A table holding only a total
has no children, so nothing is dropped. Across several hierarchical
dimensions a row is detail only when it is a leaf on *every* one (AND) — the
safe grain for the cross — so ``"only"`` is the exact complement.

Only the dimension axes (``category`` / ``area``) range over the selection:
``time`` granularity is #33's concern, a ``meta-axis`` is the pivot's, and a
``value`` axis carries no code hierarchy.
"""
from __future__ import annotations

from typing import Any

import pytest

from pyestat._endpoint import ClassObj
from pyestat._engine.aggregate import select_rows
from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)


# --- fixtures --------------------------------------------------------------


def _axis(axis_id: str, name: str, *members: dict[str, Any]) -> ClassObj:
    return ClassObj(id=axis_id, name=name, classes=tuple(members))


def _clf(*roles: tuple[str, AxisRole]) -> TableClassification:
    """A classification assigning each (axis_id, role) at high confidence —
    the selection only reads the role, not the tier."""
    return TableClassification(
        tuple(
            AxisClassification(axis_id=aid, role=role, confidence=Confidence.HIGH, signals=())
            for aid, role in roles
        )
    )


def _row(**axes: str) -> dict[str, Any]:
    return {**axes, "value": "1"}


# A 用途分類-style category hierarchy: 総数 over 食料 over its 品目 leaves.
# Parent codes present in the response → {"0", "1"}; so 総数/食料 are
# aggregates and 米/パン are leaves.
_CATEGORY_HIER = _axis(
    "cat01", "用途分類",
    {"code": "0", "name": "総数", "level": "1"},
    {"code": "1", "name": "食料", "level": "2", "parentCode": "0"},
    {"code": "11", "name": "米", "level": "3", "parentCode": "1"},
    {"code": "12", "name": "パン", "level": "3", "parentCode": "1"},
)
_TIME = _axis("time", "時間軸（年次）", {"code": "2020000000", "name": "2020年"})

_HIER_ROWS = (
    _row(cat01="0", time="2020000000"),
    _row(cat01="1", time="2020000000"),
    _row(cat01="11", time="2020000000"),
    _row(cat01="12", time="2020000000"),
)
_HIER_CLF = _clf(("cat01", AxisRole.CATEGORY), ("time", AxisRole.TIME))


def _codes(rows: tuple[dict[str, Any], ...], axis: str = "cat01") -> list[str]:
    return [r[axis] for r in rows]


# --- the headline rule: exclude / only partition by leaf-ness --------------


class TestLeafSelection:
    def test_exclude_keeps_only_the_leaves(self) -> None:
        # The double-counting fix: drop every code that has children here, so
        # only the finest grain (米 / パン) remains — summable without
        # counting 食料 and 総数 on top of their members.
        out = select_rows(_HIER_ROWS, _HIER_CLF, [_CATEGORY_HIER, _TIME], "exclude")
        assert _codes(out) == ["11", "12"]

    def test_only_keeps_the_aggregates(self) -> None:
        # The complement: the subtotal (食料) and the grand total (総数), for a
        # caller who wants the rolled-up figures rather than the detail.
        out = select_rows(_HIER_ROWS, _HIER_CLF, [_CATEGORY_HIER, _TIME], "only")
        assert _codes(out) == ["0", "1"]

    def test_include_returns_every_row_unchanged(self) -> None:
        # The default preserves today's behavior exactly — same rows, same
        # order, no filtering — so the option is backward compatible.
        out = select_rows(_HIER_ROWS, _HIER_CLF, [_CATEGORY_HIER, _TIME], "include")
        assert out == _HIER_ROWS


# --- detection is per-response, driven only by @parentCode -----------------


class TestDetectionFromParentCode:
    def test_a_flat_axis_has_no_detectable_aggregates(self) -> None:
        # 男女別 (総数 / 男 / 女) with no @parentCode links: the hierarchy is
        # not encoded, so the deterministic rule finds no aggregate. exclude
        # keeps all rows (nothing to drop); only keeps none.
        flat = _axis(
            "cat02", "男女別",
            {"code": "0", "name": "総数", "level": "1"},
            {"code": "1", "name": "男", "level": "1"},
            {"code": "2", "name": "女", "level": "1"},
        )
        rows = (_row(cat02="0"), _row(cat02="1"), _row(cat02="2"))
        clf = _clf(("cat02", AxisRole.CATEGORY))
        assert select_rows(rows, clf, [flat], "exclude") == rows
        assert select_rows(rows, clf, [flat], "only") == ()

    def test_a_lone_total_has_no_children_so_counts_as_detail(self) -> None:
        # When only 総数 is fetched (no children present), it names no one as a
        # parent → it is a leaf here. exclude keeps it (there is no
        # double-counting with a single grain); only drops it.
        rows = (_row(cat01="0", time="2020000000"),)
        assert select_rows(rows, _HIER_CLF, [_CATEGORY_HIER, _TIME], "exclude") == rows
        assert select_rows(rows, _HIER_CLF, [_CATEGORY_HIER, _TIME], "only") == ()


# --- which axes the selection ranges over ----------------------------------


class TestAxisScope:
    def test_area_hierarchy_is_filtered(self) -> None:
        # 全国 (parent of the prefectures) is an aggregate; exclude drops it so
        # a prefecture sum does not double-count the national total.
        area = _axis(
            "area", "地域",
            {"code": "00000", "name": "全国", "level": "1"},
            {"code": "13000", "name": "東京都", "level": "2", "parentCode": "00000"},
            {"code": "27000", "name": "大阪府", "level": "2", "parentCode": "00000"},
        )
        rows = (_row(area="00000"), _row(area="13000"), _row(area="27000"))
        clf = _clf(("area", AxisRole.AREA))
        assert _codes(select_rows(rows, clf, [area], "exclude"), "area") == ["13000", "27000"]
        assert _codes(select_rows(rows, clf, [area], "only"), "area") == ["00000"]

    def test_meta_axis_hierarchy_is_left_to_the_pivot(self) -> None:
        # trade's cat02 carries @parentCode (合計_金額 over its months), but as a
        # meta-axis it is the pivot's to fold (#37) — the aggregate selection
        # must not touch it, or it would drop the 合計 rows a pivot needs.
        meta = _axis(
            "cat02", "数量・金額",
            {"code": "140", "name": "合計_金額", "level": "1"},
            {"code": "150", "name": "1月_金額", "level": "2", "parentCode": "140"},
            {"code": "160", "name": "2月_金額", "level": "2", "parentCode": "140"},
        )
        rows = (_row(cat02="140"), _row(cat02="150"), _row(cat02="160"))
        clf = _clf(("cat02", AxisRole.META_AXIS))
        assert select_rows(rows, clf, [meta], "exclude") == rows
        assert select_rows(rows, clf, [meta], "only") == ()


# --- several hierarchies: detail is leaf on every one (AND) ----------------


class TestMultipleHierarchies:
    def _setup(self) -> tuple[list[ClassObj], TableClassification]:
        # 建築主 (公共 / 民間) × 用途 (総数 → 居住用 leaves) — both hierarchical.
        owner = _axis(
            "cat01", "建築主",
            {"code": "T", "name": "総数", "level": "1"},
            {"code": "P", "name": "公共", "level": "2", "parentCode": "T"},
            {"code": "Q", "name": "民間", "level": "2", "parentCode": "T"},
        )
        use = _axis(
            "cat02", "用途",
            {"code": "0", "name": "総数", "level": "1"},
            {"code": "1", "name": "居住用", "level": "2", "parentCode": "0"},
        )
        return [owner, use], _clf(
            ("cat01", AxisRole.CATEGORY), ("cat02", AxisRole.CATEGORY)
        )

    def test_detail_requires_a_leaf_on_both_axes(self) -> None:
        # Only (公共 or 民間) × 居住用 is pure detail; any row that is a total on
        # either axis is an aggregate and dropped by exclude.
        objs, clf = self._setup()
        rows = (
            _row(cat01="T", cat02="0"),  # total × total
            _row(cat01="P", cat02="0"),  # 公共 × total
            _row(cat01="T", cat02="1"),  # total × 居住用
            _row(cat01="P", cat02="1"),  # 公共 × 居住用 — the only pure detail
            _row(cat01="Q", cat02="1"),  # 民間 × 居住用 — pure detail
        )
        out = select_rows(rows, clf, objs, "exclude")
        assert [(r["cat01"], r["cat02"]) for r in out] == [("P", "1"), ("Q", "1")]

    def test_only_is_the_exact_complement_of_exclude(self) -> None:
        # exclude (pure detail) and only (aggregate on at least one axis)
        # partition the rows: together they are every row, with no overlap.
        objs, clf = self._setup()
        rows = (
            _row(cat01="T", cat02="0"),
            _row(cat01="P", cat02="0"),
            _row(cat01="P", cat02="1"),
        )
        detail = select_rows(rows, clf, objs, "exclude")
        aggregate = select_rows(rows, clf, objs, "only")
        assert set(map(id, detail)).isdisjoint(map(id, aggregate))
        assert len(detail) + len(aggregate) == len(rows)


class TestValidation:
    def test_unknown_selection_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="aggregates"):
            select_rows(_HIER_ROWS, _HIER_CLF, [_CATEGORY_HIER, _TIME], "leaf")  # type: ignore[arg-type]
