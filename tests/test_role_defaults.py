"""Tests for the role-default registry and v2 transform registry (#22).

This is the Layer A substance: the named transforms a v2 rule may
reference, and the per-role defaults that fill a short-form rule's gaps.
The hard requirement carried over from the design discussion is that
the *defaults* are total functions — a rule built purely from them (a
Layer A generic rule) can never raise at apply time, so the auto path
always has output to return.
"""
from __future__ import annotations

from pyestat._engine.apply import apply_v2_rule
from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)
from pyestat._engine.role_defaults import (
    TRANSFORMS,
    build_generic_rule,
    default_transform,
)


class TestTransformRegistry:
    def test_minimum_registered_transforms(self) -> None:
        # Done scopes the registry to "passthrough + the existing time
        # parsers"; #4 adds iso8601 / jis_x_0401 later. Pinning the floor
        # so that expansion does not depend on transforms not yet shipped.
        names = set(TRANSFORMS.names())
        assert {"passthrough", "monthly_e_stat", "quarterly_e_stat", "yearly",
                "best_effort_time"} <= names

    def test_passthrough_returns_input_unchanged(self) -> None:
        assert TRANSFORMS.resolve("passthrough")("ＮＯ") == "ＮＯ"

    def test_named_time_parser_emits_normalized_string(self) -> None:
        # Decision 3: a v2 time column yields the normalized string only
        # (the v1 companion _code / _granularity columns are #29's job).
        assert TRANSFORMS.resolve("yearly")("2020000000") == "2020"
        assert TRANSFORMS.resolve("monthly_e_stat")("2020000505") == "2020-05"


class TestBestEffortTimeIsTotal:
    def test_recognised_codes_normalize(self) -> None:
        be = TRANSFORMS.resolve("best_effort_time")
        assert be("2020000505") == "2020-05"   # monthly
        assert be("2020000103") == "2020-Q1"   # quarterly
        assert be("2020000000") == "2020"      # yearly

    def test_unrecognised_code_is_returned_raw_not_raised(self) -> None:
        # Totality is the load-bearing property: the time role-default
        # never raises, so a Layer A generic rule applied to a weird code
        # degrades to the raw value instead of blowing up the request.
        be = TRANSFORMS.resolve("best_effort_time")
        assert be("not-a-date") == "not-a-date"

    def test_non_string_input_is_returned_raw_not_raised(self) -> None:
        # Totality must hold for non-string cells too. e-Stat codes arrive
        # as strings, but an int year (a JSON/YAML layer coercing a bare
        # 4-digit code, or a caller-built row) must degrade to its raw
        # value — never a TypeError out of the regex parsers, which would
        # break the Layer A "never lose structure" guarantee.
        be = TRANSFORMS.resolve("best_effort_time")
        assert be(2020) == 2020
        assert be(None) is None


class TestRoleDefaults:
    def test_time_defaults_to_best_effort(self) -> None:
        assert default_transform(AxisRole.TIME) == "best_effort_time"

    def test_other_roles_default_to_passthrough(self) -> None:
        for role in (AxisRole.AREA, AxisRole.VALUE, AxisRole.CATEGORY,
                     AxisRole.META_AXIS, AxisRole.UNKNOWN):
            assert default_transform(role) == "passthrough"

    def test_every_role_default_is_a_total_transform(self) -> None:
        # The Layer A safety guarantee, asserted structurally: every
        # role-default resolves to a transform that returns (does not
        # raise) on arbitrary, non-conforming input — string or not.
        for role in AxisRole:
            transform = TRANSFORMS.resolve(default_transform(role))
            for adversarial in ("???garbage???", 2020, None, 3.14):
                transform(adversarial)  # must not raise


def _axis(axis_id: str, role: AxisRole) -> AxisClassification:
    return AxisClassification(axis_id, role, Confidence.HIGH, ("test",))


class TestBuildGenericRule:
    """``build_generic_rule`` turns a classification into a Layer A rule, or
    declines (``None``) when the table cannot be structured generically and
    must route to Layer D.

    Business rule: Layer A only handles tables where every axis carries a
    distinct, directly-addressable role. A meta-axis (needs the #10 pivot),
    an ``unknown`` axis (the classifier's route-to-D sentinel), or a role
    that repeats across axes (disambiguating needs #10's where-predicate)
    each make the table ineligible, so it rides Layer D instead.
    """

    def test_clean_single_axis_table_yields_one_column_per_axis(self) -> None:
        clf = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("area", AxisRole.AREA),
            _axis("tab", AxisRole.VALUE),
        ))
        rule = build_generic_rule(clf)
        assert rule is not None
        assert rule.schema_version == "2"
        assert list(rule.match.role_pattern) == [
            AxisRole.TIME, AxisRole.AREA, AxisRole.VALUE,
        ]
        # One column per axis: the value role becomes the "value" column and
        # the rest read their axis, each with its role-default transform.
        cols = {c.column: c for c in rule.output}
        assert set(cols) == {"time", "area", "value"}
        assert cols["time"].source.role == AxisRole.TIME
        assert cols["time"].transform == "best_effort_time"
        assert cols["area"].source.role == AxisRole.AREA
        assert cols["area"].transform == "passthrough"
        assert cols["value"].source.role == AxisRole.VALUE
        assert cols["value"].transform == "passthrough"

    def test_built_rule_is_directly_applicable(self) -> None:
        # What Layer A builds is exactly what it hands to apply_v2_rule, so
        # the rule must apply without a separate load/expand step.
        clf = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("tab", AxisRole.VALUE),
        ))
        rule = build_generic_rule(clf)
        assert rule is not None
        rows = ({"time": "2020000000", "tab": "020", "value": "126146"},)
        # The built rule applies directly, emitting canonical cells (#35):
        # a time object and a {value,unit} measure (no unit on this row).
        assert apply_v2_rule(rows, clf, rule) == ({
            "time": {"code": "2020000000", "label": "2020000000",
                     "normalized": "2020", "granularity": "yearly"},
            "value": {"value": "126146", "unit": None},
        },)

    def test_meta_axis_declines(self) -> None:
        clf = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("tab", AxisRole.VALUE),
        ))
        assert build_generic_rule(clf) is None

    def test_unknown_axis_declines(self) -> None:
        clf = TableClassification((
            _axis("cat01", AxisRole.UNKNOWN),
            _axis("tab", AxisRole.VALUE),
        ))
        assert build_generic_rule(clf) is None

    def test_repeated_role_declines(self) -> None:
        # Population's age + sex are two category axes; picking which column
        # reads which needs #10's where-predicate, so Layer A declines.
        clf = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("cat01", AxisRole.CATEGORY),
            _axis("cat03", AxisRole.CATEGORY),
            _axis("tab", AxisRole.VALUE),
        ))
        assert build_generic_rule(clf) is None

    def test_empty_classification_declines(self) -> None:
        assert build_generic_rule(TableClassification(())) is None

    def test_value_column_name_collision_declines(self) -> None:
        # A non-value axis whose id is literally "value" would collide with
        # the value role's "value" column. Decline (→ Layer D) rather than
        # let RuleV2's duplicate-column check raise on the auto path.
        clf = TableClassification((
            _axis("value", AxisRole.CATEGORY),
            _axis("tab", AxisRole.VALUE),
        ))
        assert build_generic_rule(clf) is None
