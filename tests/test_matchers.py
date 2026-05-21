"""Tests for the Layer-3 Matcher pipeline.

The pipeline narrows candidate rules by statsCode, then validates the
match structurally. Each Matcher is tested in isolation so a future
addition (TableIdExactMatcher, NamePatternMatcher) can append without
touching the existing ones.
"""
from __future__ import annotations

import pytest

from pyestat._endpoint import ClassObj, StatsDataResponse
from pyestat._matchers import FingerprintMatcher, StatsCodeMatcher
from pyestat._rule import Rule


def _resp(
    *,
    stat_code: str | None = "00200524",
    axes: tuple[tuple[str, str], ...] = (("tab", "T"), ("time", "時間軸（年次）")),
) -> StatsDataResponse:
    """Build a response with the few fields the matchers actually consult."""
    table_inf: dict = {}
    if stat_code is not None:
        table_inf["STAT_NAME"] = {"@code": stat_code, "$": "x"}
    return StatsDataResponse(
        stats_data_id="X",
        total_number=None,
        table_inf=table_inf,
        class_objs=tuple(ClassObj(id=i, name=n, classes=()) for i, n in axes),
        values=(),
    )


def _rule(*, statsCode: str = "00200524", time_id: str = "time", area_id: str | None = None) -> Rule:
    axes: dict = {"time": {"id": time_id, "format": "yearly"}}
    if area_id is not None:
        axes["area"] = {"id": area_id}
    return Rule.model_validate(
        {
            "schema_version": "1",
            "match": {"statsCode": statsCode},
            "axes": axes,
            "value": {"type": "number"},
        }
    )


class TestStatsCodeMatcher:
    def test_matches_on_equal_stat_name_code(self) -> None:
        # statsCode is the cheap narrowing step DESIGN.md commits to;
        # it reads from TABLE_INF.STAT_NAME.@code where e-Stat keeps
        # the statistic-family code (separate from statsDataId).
        assert StatsCodeMatcher().matches(_resp(stat_code="00200524"), _rule(statsCode="00200524"))

    def test_rejects_on_mismatched_code(self) -> None:
        assert not StatsCodeMatcher().matches(_resp(stat_code="00200524"), _rule(statsCode="00100409"))

    def test_rejects_when_response_lacks_stat_name(self) -> None:
        # Some search-result-only tables omit STAT_NAME; a rule with a
        # specific statsCode must not silently match such a response,
        # because the @code field is the only thing the rule was
        # narrowing on.
        assert not StatsCodeMatcher().matches(_resp(stat_code=None), _rule(statsCode="00200524"))

    def test_handles_stat_name_as_bare_string(self) -> None:
        # TABLE_INF schema drift: STAT_NAME has been seen as a bare
        # string rather than a {@code, $} dict (DESIGN.md context
        # section). The matcher must not crash and must report False
        # because the @code is inaccessible.
        resp = StatsDataResponse(
            stats_data_id="X",
            total_number=None,
            table_inf={"STAT_NAME": "人口推計"},  # bare string
            class_objs=(),
            values=(),
        )
        assert not StatsCodeMatcher().matches(resp, _rule(statsCode="00200524"))


class TestFingerprintMatcher:
    def test_matches_when_response_has_the_axes_the_rule_names(self) -> None:
        # The minimum claim a rule makes is "I'll read these axes";
        # the fingerprint matcher refuses rules whose claim does not
        # line up with the table actually under inspection.
        assert FingerprintMatcher().matches(
            _resp(axes=(("tab", "T"), ("time", "時間軸"))),
            _rule(time_id="time"),
        )

    def test_rejects_when_named_axis_missing(self) -> None:
        # If the rule says ``axes.time.id = "time"`` but the response
        # has no ``time`` axis, applying the rule would crash at
        # transform time; refusing here saves the row stream.
        assert not FingerprintMatcher().matches(
            _resp(axes=(("tab", "T"),)),
            _rule(time_id="time"),
        )

    def test_optional_area_axis_must_be_present_when_named(self) -> None:
        # Area is optional in the rule schema. When supplied, it joins
        # the set of axes that must exist on the response.
        assert FingerprintMatcher().matches(
            _resp(axes=(("time", "X"), ("area", "Y"))),
            _rule(time_id="time", area_id="area"),
        )
        assert not FingerprintMatcher().matches(
            _resp(axes=(("time", "X"),)),
            _rule(time_id="time", area_id="area"),
        )

    def test_extra_response_axes_are_fine(self) -> None:
        # A rule does not need to enumerate every axis on the table —
        # only the ones it touches. Extras (cat01, cat02, …) are
        # consumed unchanged by Layer 3 and must not block a match.
        assert FingerprintMatcher().matches(
            _resp(axes=(("tab", "T"), ("cat01", "C"), ("time", "X"))),
            _rule(time_id="time"),
        )
