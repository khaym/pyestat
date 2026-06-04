"""Tests for the v2 auto-path rule resolver (#28).

``resolve_v2`` chooses the rule to apply from a table's classification,
walking Layers C (user > project) > B (builtin) > A (generic). It returns
``None`` to signal "route to Layer D" when the classification is too weak
to trust or when no rule — specific or generic — could be produced. The
classifier output is built by hand here so resolution is exercised in
isolation (the request-path plumbing is the endpoint's job).
"""
from __future__ import annotations

import pytest

from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)
from pyestat._engine.resolver import resolve_v2
from pyestat._engine.rule import RuleV2
from pyestat.errors import AmbiguousRuleError


def _axis(
    axis_id: str, role: AxisRole, confidence: Confidence = Confidence.HIGH
) -> AxisClassification:
    return AxisClassification(axis_id, role, confidence, ("test",))


# A clean time + area + value table — every role appears once and is
# confident, so a generic rule can always be built for it.
_CLEAN = TableClassification((
    _axis("time", AxisRole.TIME),
    _axis("area", AxisRole.AREA),
    _axis("tab", AxisRole.VALUE),
))


def _rule(role_pattern: list[str], *, tag: str) -> RuleV2:
    # ``tag`` is the output column name so a test can tell which rule won.
    return RuleV2.model_validate({
        "schema_version": "2",
        "match": {"role_pattern": role_pattern},
        "output": [{"column": tag, "source": {"role": "value"}, "transform": "passthrough"}],
    })


class TestSpecificRuleMatch:
    def test_builtin_matches_by_role_pattern(self) -> None:
        rule = _rule(["time", "area", "value"], tag="builtin")
        assert resolve_v2(_CLEAN, builtin=[rule]) is rule

    def test_non_matching_pattern_is_ignored_and_generic_is_built(self) -> None:
        # A builtin for a different pattern (no area) must not fire; the
        # clean table then falls through to a Layer A generic rule.
        rule = _rule(["time", "value"], tag="builtin")
        result = resolve_v2(_CLEAN, builtin=[rule])
        assert result is not rule
        assert result is not None


class TestPrecedence:
    """Resolution order is user (C) > project (C) > builtin (B)."""

    def test_user_shadows_builtin_for_same_pattern(self) -> None:
        user = _rule(["time", "area", "value"], tag="user")
        builtin = _rule(["time", "area", "value"], tag="builtin")
        assert resolve_v2(_CLEAN, user=[user], builtin=[builtin]) is user

    def test_project_shadows_builtin(self) -> None:
        project = _rule(["time", "area", "value"], tag="project")
        builtin = _rule(["time", "area", "value"], tag="builtin")
        assert resolve_v2(_CLEAN, project=[project], builtin=[builtin]) is project

    def test_user_shadows_project(self) -> None:
        user = _rule(["time", "area", "value"], tag="user")
        project = _rule(["time", "area", "value"], tag="project")
        assert resolve_v2(_CLEAN, user=[user], project=[project]) is user


class TestAmbiguity:
    def test_two_matches_in_same_layer_raise(self) -> None:
        # Two builtins claiming the same role pattern is an authoring
        # conflict surfaced loudly, not silently resolved.
        rules = [
            _rule(["time", "area", "value"], tag="a"),
            _rule(["time", "area", "value"], tag="b"),
        ]
        with pytest.raises(AmbiguousRuleError) as exc:
            resolve_v2(_CLEAN, builtin=rules, stats_data_id="X")
        assert exc.value.stats_data_id == "X"
        assert len(exc.value.matched_rules) == 2


class TestLayerAFallback:
    def test_no_match_builds_generic_for_clean_table(self) -> None:
        result = resolve_v2(_CLEAN)
        assert result is not None
        assert list(result.match.role_pattern) == [
            AxisRole.TIME, AxisRole.AREA, AxisRole.VALUE,
        ]

    def test_no_match_and_no_generic_routes_to_layer_d(self) -> None:
        # A meta-axis table: no specific rule and build_generic declines,
        # so resolution returns None (the endpoint then runs Layer D).
        meta = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("tab", AxisRole.VALUE),
        ))
        assert resolve_v2(meta) is None


class TestThresholdGate:
    def test_low_confidence_axis_routes_to_layer_d(self) -> None:
        # A low-confidence axis means the role pattern itself is unreliable,
        # so the table goes to Layer D even though a rule would match.
        weak = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("area", AxisRole.AREA),
            _axis("tab", AxisRole.VALUE, confidence=Confidence.LOW),
        ))
        builtin = _rule(["time", "area", "value"], tag="builtin")
        assert resolve_v2(weak, builtin=[builtin]) is None
