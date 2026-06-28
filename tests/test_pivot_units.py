"""Tests for the pivot's isolated business rules.

The pivot folds meta-axis-spread rows into one record per non-meta group.
``_apply_pivot`` decomposes into the units tested here — selecting the single
member a predicate matches, deriving a sub-grain from member names, sub-grouping
by that grain, and indexing meta members. Each encodes one rule a caller relies
on, exercised directly rather than only through the whole fold.
"""
from __future__ import annotations

import re

import pytest

from pyestat._endpoint import ClassObj
from pyestat._engine.apply import (
    _MemberIndex,
    _PivotPlan,
    _build_member_index,
    _grain_of,
    _resolve_meta_axis,
    _select_one_member,
    _subgroup_by_grain,
)
from pyestat._engine.classifier import AxisRole
from pyestat._errors import RoleResolutionError


def _err(n: int) -> RoleResolutionError:
    return RoleResolutionError(role="meta-axis", reason=f"{n} distinct members")


class TestSelectOneMember:
    """A `where` / `unit_from` selects exactly one meta member within its pool;
    counting *distinct* members, not rows."""

    rows = [
        {"m": "a", "value": "1"},
        {"m": "b", "value": "2"},
        {"m": "a", "value": "3"},  # member 'a' appears twice
    ]

    def test_no_match_yields_none(self) -> None:
        assert _select_one_member(lambda c: c == "z", self.rows, "m", _err) is None

    def test_one_match_returns_that_row(self) -> None:
        assert _select_one_member(lambda c: c == "b", self.rows, "m", _err) == {
            "m": "b",
            "value": "2",
        }

    def test_the_same_member_repeated_collapses_to_its_first_row(self) -> None:
        # A member duplicated within the pool is not ambiguity — take the first.
        assert _select_one_member(lambda c: c == "a", self.rows, "m", _err) == {
            "m": "a",
            "value": "1",
        }

    def test_several_distinct_members_is_ambiguity_the_caller_must_narrow(self) -> None:
        with pytest.raises(RoleResolutionError, match="2 distinct members"):
            _select_one_member(lambda c: c in {"a", "b"}, self.rows, "m", _err)


# Trade encodes the month only in the member *name* ("1月_金額"); a `key`
# pattern lifts it into the grain. 総数 carries no month — it forms no row.
_INDEX = _MemberIndex(
    name_by_code={"x1": "1月_金額", "x2": "2月_金額", "tot": "総数"},
    parent_name_by_code={"x1": None, "x2": None, "tot": None},
    level_by_code={"x1": "2", "x2": "2", "tot": "1"},
)
_KEY_PLAN = [("month", re.compile(r"(\d+)月"))]


def _plan(*, meta_id: str = "m", key_plan=_KEY_PLAN) -> _PivotPlan:
    """A minimal pivot plan exercising only the grain-deriving fields; the
    grouping / non-meta / where plans are irrelevant to these units."""
    return _PivotPlan(
        meta_id=meta_id,
        index=_INDEX,
        group_axis_ids=[],
        nonmeta_plan=[],
        key_plan=list(key_plan),
        where_plan=[],
    )


class TestGrainOf:
    def test_derives_the_first_capture_group_from_the_member_name(self) -> None:
        assert _grain_of({"m": "x1"}, _plan()) == ("1",)

    def test_uses_the_whole_match_when_the_pattern_declares_no_group(self) -> None:
        assert _grain_of({"m": "x2"}, _plan(key_plan=[("k", re.compile(r"\d+月"))])) == ("2月",)

    def test_a_member_the_pattern_does_not_match_yields_none(self) -> None:
        # 総数 sits outside any derived grain.
        assert _grain_of({"m": "tot"}, _plan()) == (None,)


class TestSubgroupByGrain:
    rows = [{"m": "x1"}, {"m": "x2"}, {"m": "tot"}]

    def test_no_key_plan_is_one_grain_less_record(self) -> None:
        assert _subgroup_by_grain(self.rows, _plan(key_plan=[])) == [((), self.rows)]

    def test_groups_by_derived_grain_and_drops_members_outside_it(self) -> None:
        out = dict(_subgroup_by_grain(self.rows, _plan()))
        assert out == {("1",): [{"m": "x1"}], ("2",): [{"m": "x2"}]}  # 総数 dropped


class TestResolveMetaAxis:
    objs = (
        ClassObj(id="cat01", name="品目", classes=({"code": "x1", "name": "1月_金額"},)),
    )

    def test_returns_the_single_meta_axis_and_its_members(self) -> None:
        meta_id, members = _resolve_meta_axis({AxisRole.META_AXIS: ["cat01"]}, self.objs)
        assert meta_id == "cat01"
        assert members["x1"]["name"] == "1月_金額"

    def test_zero_or_several_meta_axes_is_not_pivotable(self) -> None:
        with pytest.raises(RoleResolutionError, match="exactly one meta-axis"):
            _resolve_meta_axis({AxisRole.META_AXIS: ["a", "b"]}, self.objs)
        with pytest.raises(RoleResolutionError, match="exactly one meta-axis"):
            _resolve_meta_axis({}, self.objs)

    def test_a_pivot_needs_class_metadata_to_match_members_by_name(self) -> None:
        with pytest.raises(RoleResolutionError, match="needs class metadata"):
            _resolve_meta_axis({AxisRole.META_AXIS: ["cat01"]}, None)


class TestBuildMemberIndex:
    def test_resolves_parent_to_its_name_and_keeps_level_as_a_string(self) -> None:
        members = {
            "0": {"code": "0", "name": "総数", "level": "1"},
            "1": {"code": "1", "name": "食料", "level": "2", "parentCode": "0"},
        }
        index = _build_member_index(members)
        assert index.parent_name_by_code["1"] == "総数"  # parent code → parent name
        assert index.parent_name_by_code["0"] is None  # a root has no parent
        assert index.level_by_code["1"] == "2"
