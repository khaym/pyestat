"""Tests for the Layer-3 RuleManager — rule selection orchestrator.

The manager walks the candidate rule list through the matcher pipeline,
respecting the three-layer resolution order (Decision E):

    user > project > builtin

— and raises :class:`AmbiguousRuleError` when two rules at the *same*
precedence both match.
"""
from __future__ import annotations

import pytest

from pyestat._endpoint import ClassObj, StatsDataResponse
from pyestat._rule import Rule
from pyestat._rule_manager import RuleManager
from pyestat.errors import AmbiguousRuleError


def _resp(stat_code: str = "00200524") -> StatsDataResponse:
    return StatsDataResponse(
        stats_data_id="X",
        total_number=None,
        table_inf={"STAT_NAME": {"@code": stat_code, "$": "x"}},
        class_objs=(ClassObj(id="time", name="時間軸", classes=()),),
        values=(),
    )


def _rule(stat_code: str = "00200524", time_id: str = "time", marker: str = "default") -> Rule:
    # ``marker`` is just a way to tell two otherwise-identical rules
    # apart in assertions; pydantic ignores it because of extra=forbid,
    # so we encode the marker into statsCode instead.
    return Rule.model_validate(
        {
            "schema_version": "1",
            "match": {"statsCode": stat_code},
            "axes": {"time": {"id": time_id, "format": "yearly"}},
            "value": {"type": "number"},
        }
    )


class TestRuleSelection:
    def test_returns_unique_match(self) -> None:
        # The base case: one rule, one match.
        rules = [_rule(stat_code="00200524")]
        assert RuleManager(builtin=rules).select(_resp(stat_code="00200524")) is rules[0]

    def test_returns_none_when_no_rule_matches(self) -> None:
        # No-rule responses fall through to caller-side fallback
        # (Decision B's ``rule=None`` raw mode); the manager itself
        # signals "nothing applies" by returning ``None``.
        rules = [_rule(stat_code="00200524")]
        assert RuleManager(builtin=rules).select(_resp(stat_code="00100409")) is None


class TestResolutionOrder:
    """Decision E pins the precedence chain user > project > builtin.
    Earlier layers shadow later ones so a consumer can override a
    bundled rule without forking the library."""

    def test_user_layer_wins_over_project_and_builtin(self) -> None:
        user_rule = _rule()
        project_rule = _rule()
        builtin_rule = _rule()
        chosen = RuleManager(
            user=[user_rule], project=[project_rule], builtin=[builtin_rule]
        ).select(_resp())
        assert chosen is user_rule

    def test_project_layer_wins_over_builtin(self) -> None:
        project_rule = _rule()
        builtin_rule = _rule()
        chosen = RuleManager(project=[project_rule], builtin=[builtin_rule]).select(_resp())
        assert chosen is project_rule

    def test_lower_layer_used_when_higher_layer_misses(self) -> None:
        # Shadowing only happens when the higher layer *matches*;
        # otherwise the chain proceeds. Otherwise an unrelated
        # user-rule for a different table would block every builtin
        # rule from ever firing.
        project_rule_for_different_table = _rule(stat_code="00100409")
        builtin_rule = _rule(stat_code="00200524")
        chosen = RuleManager(
            project=[project_rule_for_different_table], builtin=[builtin_rule]
        ).select(_resp(stat_code="00200524"))
        assert chosen is builtin_rule


class TestAmbiguity:
    def test_two_same_layer_matches_raise(self) -> None:
        # Two rules at the same precedence both claiming the same
        # table is a rule-authoring mistake we want surfaced loudly,
        # not silently picked one of the two.
        rules = [_rule(stat_code="00200524"), _rule(stat_code="00200524")]
        with pytest.raises(AmbiguousRuleError) as exc:
            RuleManager(builtin=rules).select(_resp(stat_code="00200524"))
        assert exc.value.stats_data_id == "X"
        assert len(exc.value.matched_rules) == 2

    def test_match_at_a_higher_layer_overrides_ambiguity_at_a_lower_layer(self) -> None:
        # A user override resolves an ambiguity buried in the
        # builtins; the manager never has to disambiguate the
        # builtins because the user layer terminates the walk first.
        user_rule = _rule()
        builtin_ambiguous = [_rule(), _rule()]
        chosen = RuleManager(user=[user_rule], builtin=builtin_ambiguous).select(_resp())
        assert chosen is user_rule
