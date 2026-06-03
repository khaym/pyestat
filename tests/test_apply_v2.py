"""Tests for applying a v2 rule to fetched rows (task #22).

``apply_v2_rule`` is the v2 counterpart of the v1 ``apply_rule`` path.
It takes the rows, the axis *classification* (which axis plays which
role), and a v2 rule, and emits one output row per input row with the
declared columns. Resolving role → axis from a classification is the
seam with #28: #28 runs the classifier on the request path and hands
the result here; these tests build the classification by hand so the
apply logic is exercised in isolation.

Pivot (a role mapping to several axes, disambiguated by a ``where``
predicate) is #10 and explicitly out of scope; here a referenced role
must resolve to exactly one axis or the call fails identifiably so #28
can fall back to Layer D.
"""
from __future__ import annotations

import pytest

from pyestat._engine.apply import apply_v2_rule
from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)
from pyestat._engine.rule import RuleV2
from pyestat.errors import RoleResolutionError


def _axis(axis_id: str, role: AxisRole) -> AxisClassification:
    return AxisClassification(axis_id, role, Confidence.HIGH, ("test",))


# A time+area+value table: e-Stat ships the observation under "value"
# and the axis codes under their axis ids (Layer 2's flattened form).
_CLASSIFICATION = TableClassification((
    _axis("time", AxisRole.TIME),
    _axis("area", AxisRole.AREA),
    _axis("tab", AxisRole.VALUE),
))

_ROWS = (
    {"time": "2020000000", "area": "13000", "tab": "020", "value": "123"},
    {"time": "2021000000", "area": "27000", "tab": "020", "value": "456"},
)


def _rule(output: list[dict]) -> RuleV2:
    return RuleV2.model_validate({
        "schema_version": "2",
        "match": {"role_pattern": ["time", "area", "value"]},
        "output": output,
    })


class TestApplyV2LongForm:
    def test_emits_declared_columns_with_transforms_applied(self) -> None:
        # The core Done: long-form column declarations drive the output.
        # time is normalized by its transform, area passes through, and
        # the value role reads the cell, not an axis code.
        rule = _rule([
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
            {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
        ])
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert out == (
            {"time": "2020", "area": "13000", "value": "123"},
            {"time": "2021", "area": "27000", "value": "456"},
        )

    def test_value_role_reads_the_cell_not_the_tab_axis_code(self) -> None:
        # The VALUE role is special: its source is the observation cell
        # ("value"), even though the classifier assigns the role to the
        # single-member tab axis. A column drawing on it must surface 123,
        # not the tab code "020".
        rule = _rule([{"column": "v", "source": {"role": "value"}, "transform": "passthrough"}])
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert out[0] == {"v": "123"}


class TestApplyV2ShortForm:
    def test_accepts_short_form_by_expanding_defensively(self) -> None:
        # apply expands internally, so a caller (e.g. #28 building a Layer
        # A rule in memory) can pass a short-form rule without a separate
        # load step.
        rule = _rule([{"column": "time"}, {"column": "area"}, {"column": "value"}])
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert out[0] == {"time": "2020", "area": "13000", "value": "123"}


class TestApplyV2RoleResolution:
    def test_role_absent_from_classification_raises_identifiably(self) -> None:
        # A rule asking for an area column on an area-less table cannot be
        # satisfied; the error is a typed EstatError so #28 can catch it
        # and route to Layer D rather than surfacing it to the caller.
        gdp_like = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("tab", AxisRole.VALUE),
        ))
        rule = _rule([{"column": "area", "source": {"role": "area"}, "transform": "passthrough"}])
        with pytest.raises(RoleResolutionError, match="area"):
            apply_v2_rule(_ROWS, gdp_like, rule)

    def test_role_mapping_to_multiple_axes_points_at_pivot(self) -> None:
        # Two category axes and a column drawing on "category" is the
        # pivot case (#10): without a where predicate the engine cannot
        # pick one. Fail identifiably and name the reason.
        two_cats = TableClassification((
            _axis("cat01", AxisRole.CATEGORY),
            _axis("cat02", AxisRole.CATEGORY),
            _axis("tab", AxisRole.VALUE),
        ))
        rule = _rule([
            {"column": "c", "source": {"role": "category"}, "transform": "passthrough"},
        ])
        with pytest.raises(RoleResolutionError, match="multiple"):
            apply_v2_rule(_ROWS, two_cats, rule)


class TestLayerAGenericRuleNeverRaises:
    def test_role_default_only_rule_degrades_instead_of_raising(self) -> None:
        # The auto-path guarantee from the design discussion: a rule built
        # purely from role-defaults (what Layer A generates) applies
        # without raising even on adversarial codes — a time code that no
        # parser recognises is preserved raw, not thrown.
        rule = _rule([{"column": "time"}, {"column": "area"}, {"column": "value"}])
        adversarial = ({"time": "garbage", "area": "??", "tab": "020", "value": "-"},)
        out = apply_v2_rule(adversarial, _CLASSIFICATION, rule)
        assert out == ({"time": "garbage", "area": "??", "value": "-"},)
