"""Tests for the v2 auto-path rule resolver.

``resolve_v2`` chooses the rule to apply from a table's classification,
walking Layers C (user > project) > B (builtin) > A (generic). It returns
``None`` to signal "route to Layer D" when the classification is too weak
to trust or when no rule — specific or generic — could be produced; a
non-``None`` result pairs the rule with the :class:`RuleLayer` it came
from, the provenance the auto path uses to decide surface-vs-degrade.
These tests pin both the chosen rule and its layer. The classifier output
is built by hand here so resolution is exercised in isolation (the
request-path plumbing is the endpoint's job).
"""
from __future__ import annotations

import pytest

from pyestat._endpoint import ClassObj
from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)
from pyestat._engine.resolver import RuleLayer, resolve_v2
from pyestat._engine.rule import RuleV2
from pyestat.errors import AmbiguousRuleError


def _axis(
    axis_id: str, role: AxisRole, confidence: Confidence = Confidence.HIGH
) -> AxisClassification:
    return AxisClassification(axis_id, role, confidence, ("test",))


def _classobj(axis_id: str, members: list[tuple[str, str]]) -> ClassObj:
    return ClassObj(
        id=axis_id,
        name=axis_id,
        classes=tuple({"code": code, "name": name} for code, name in members),
    )


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
        resolved = resolve_v2(_CLEAN, builtin=[rule])
        assert resolved is not None
        assert resolved.rule is rule
        assert resolved.layer is RuleLayer.BUILTIN

    def test_non_matching_pattern_is_ignored_and_generic_is_built(self) -> None:
        # A builtin for a different pattern (no area) must not fire; the
        # clean table then falls through to a Layer A generic rule.
        rule = _rule(["time", "value"], tag="builtin")
        result = resolve_v2(_CLEAN, builtin=[rule])
        assert result is not None
        assert result.rule is not rule
        assert result.layer is RuleLayer.GENERIC


class TestPrecedence:
    """Resolution order is user (C) > project (C) > builtin (B); the
    resolved layer reports which one won."""

    def test_user_shadows_builtin_for_same_pattern(self) -> None:
        user = _rule(["time", "area", "value"], tag="user")
        builtin = _rule(["time", "area", "value"], tag="builtin")
        resolved = resolve_v2(_CLEAN, user=[user], builtin=[builtin])
        assert resolved is not None
        assert resolved.rule is user
        assert resolved.layer is RuleLayer.USER

    def test_project_shadows_builtin(self) -> None:
        project = _rule(["time", "area", "value"], tag="project")
        builtin = _rule(["time", "area", "value"], tag="builtin")
        resolved = resolve_v2(_CLEAN, project=[project], builtin=[builtin])
        assert resolved is not None
        assert resolved.rule is project
        assert resolved.layer is RuleLayer.PROJECT

    def test_user_shadows_project(self) -> None:
        user = _rule(["time", "area", "value"], tag="user")
        project = _rule(["time", "area", "value"], tag="project")
        resolved = resolve_v2(_CLEAN, user=[user], project=[project])
        assert resolved is not None
        assert resolved.rule is user
        assert resolved.layer is RuleLayer.USER


class TestAmbiguity:
    """A same-layer conflict routes by provenance: a caller-authored
    layer surfaces it, a library layer degrades rather than crashes."""

    def test_caller_authored_conflict_surfaces(self) -> None:
        # Two user rules claiming the same role pattern is the caller's own
        # authoring conflict — surfaced loudly so they can fix it.
        rules = [
            _rule(["time", "area", "value"], tag="a"),
            _rule(["time", "area", "value"], tag="b"),
        ]
        with pytest.raises(AmbiguousRuleError) as exc:
            resolve_v2(_CLEAN, user=rules, stats_data_id="X")
        assert exc.value.stats_data_id == "X"
        assert len(exc.value.matched_rules) == 2

    def test_builtin_conflict_degrades_instead_of_raising(self) -> None:
        # Two built-ins claiming one pattern is a library packaging bug the
        # caller cannot fix; resolution skips the conflicted builtin layer and
        # falls through (here to a Layer A generic for the clean table) rather
        # than crash the caller.
        rules = [
            _rule(["time", "area", "value"], tag="a"),
            _rule(["time", "area", "value"], tag="b"),
        ]
        result = resolve_v2(_CLEAN, builtin=rules)
        assert result is not None
        assert result.layer is RuleLayer.GENERIC


class TestLayerAFallback:
    def test_no_match_builds_generic_for_clean_table(self) -> None:
        result = resolve_v2(_CLEAN)
        assert result is not None
        assert result.layer is RuleLayer.GENERIC
        assert list(result.rule.match.role_pattern) == [
            AxisRole.TIME, AxisRole.AREA, AxisRole.VALUE,
        ]

    def test_meta_axis_without_class_objs_routes_to_layer_d(self) -> None:
        # A meta-axis table can be auto-pivoted, but only with the member
        # names; with no class_objs the generic fallback declines, so
        # resolution returns None (the endpoint then runs Layer D).
        meta = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("area", AxisRole.AREA),
        ))
        assert resolve_v2(meta) is None

    def test_meta_axis_with_class_objs_builds_generic_pivot(self) -> None:
        # Given the member names, the Layer A fallback auto-generates a pivot
        # rule instead of declining — an uncovered meta-axis table now
        # resolves to a GENERIC rule that folds it rather than to Layer D.
        meta = TableClassification((
            _axis("cat01", AxisRole.CATEGORY),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("area", AxisRole.AREA),
            _axis("time", AxisRole.TIME),
        ))
        objs = (_classobj("cat02", [("130", "合計_数量2"), ("140", "合計_金額")]),)
        result = resolve_v2(meta, class_objs=objs)
        assert result is not None
        assert result.layer is RuleLayer.GENERIC
        # The meta members became where-columns — the mark of a pivot rule.
        where_cols = [c.column for c in result.rule.output if c.source.where is not None]
        assert where_cols == ["合計_数量2", "合計_金額"]


class TestStatsCodeNarrowing:
    """A rule may pin ``match.stats_code`` so it fires only on its survey
    family. ``role_pattern`` stays the authority; ``stats_code`` is an
    extra AND-narrowing that keeps a family-specific rule (whose selectors are
    tied to one survey's member names) from claiming a structurally identical
    table from another family."""

    def _scoped(self, *, tag: str, stats_code: str) -> RuleV2:
        return RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["time", "area", "value"], "stats_code": stats_code},
            "output": [{"column": tag, "source": {"role": "value"}, "transform": "passthrough"}],
        })

    def test_scoped_rule_fires_for_its_family(self) -> None:
        rule = self._scoped(tag="trade", stats_code="00350300")
        resolved = resolve_v2(_CLEAN, builtin=[rule], stats_code="00350300")
        assert resolved is not None
        assert resolved.rule is rule
        assert resolved.layer is RuleLayer.BUILTIN

    def test_scoped_rule_skips_a_different_family(self) -> None:
        # Same role pattern, different family: the rule must not fire, so the
        # clean table falls through to a Layer A generic rather than be folded
        # by a foreign family's rule.
        rule = self._scoped(tag="trade", stats_code="00350300")
        resolved = resolve_v2(_CLEAN, builtin=[rule], stats_code="00200521")
        assert resolved is not None
        assert resolved.rule is not rule
        assert resolved.layer is RuleLayer.GENERIC

    def test_scoped_rule_skips_a_table_with_no_stats_code(self) -> None:
        # When the table carries no statsCode (TABLE_INF drift), a scoped rule
        # cannot confirm the family, so it declines rather than guess.
        rule = self._scoped(tag="trade", stats_code="00350300")
        resolved = resolve_v2(_CLEAN, builtin=[rule])
        assert resolved is not None
        assert resolved.layer is RuleLayer.GENERIC

    def test_unscoped_rule_still_matches_regardless_of_family(self) -> None:
        # A rule without stats_code keeps the O(role patterns) default: it
        # matches by role pattern alone whatever family the table belongs to.
        rule = _rule(["time", "area", "value"], tag="generic_builtin")
        resolved = resolve_v2(_CLEAN, builtin=[rule], stats_code="00350300")
        assert resolved is not None
        assert resolved.rule is rule
        assert resolved.layer is RuleLayer.BUILTIN


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
