"""Tests for applying a v2 rule to fetched rows (task #22).

``apply_v2_rule`` is the v2 counterpart of the v1 ``apply_rule`` path.
It takes the rows, the axis *classification* (which axis plays which
role), and a v2 rule, and emits one output row per input row with the
declared columns. Resolving role → axis from a classification is the
seam with #28: #28 runs the classifier on the request path and hands
the result here; these tests build the classification by hand so the
apply logic is exercised in isolation.

Two shapes share this entry point:

* **1:1** — one output row per input row; a referenced role must resolve
  to exactly one axis (a non-meta role spanning several axes still fails
  identifiably so #28 can fall back to Layer D).
* **N:1 pivot (#10)** — when a column's ``meta-axis`` source carries a
  ``where`` predicate, rows are folded by the non-meta axes into one
  record per group and each predicate selects a member's cell.
"""
from __future__ import annotations

import pytest

from pyestat._endpoint import ClassObj
from pyestat._engine.apply import apply_v2_rule
from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)
from pyestat._engine.rule import RuleV2
from pyestat.errors import (
    EstatError,
    RoleResolutionError,
    RuleAuthoringError,
    RuleExpansionError,
    UnknownTransformError,
)


def _axis(axis_id: str, role: AxisRole) -> AxisClassification:
    return AxisClassification(axis_id, role, Confidence.HIGH, ("test",))


def _classobj(axis_id: str, members: list[tuple[str, str]]) -> ClassObj:
    """A ClassObj from (code, name) pairs — the meta-member name lookup the
    pivot path matches ``where`` against."""
    return ClassObj(
        id=axis_id,
        name=axis_id,
        classes=tuple({"code": code, "name": name} for code, name in members),
    )


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
        # Two category axes and a column drawing on "category" is ambiguous:
        # #10 added the meta-axis pivot, but disambiguating a *non-meta* role
        # across several axes is still out of scope. Fail identifiably and
        # name the reason.
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


class TestUnknownTransform:
    """A transform name the registry does not know is an authoring error that
    must surface as a typed :class:`UnknownTransformError` (#32) — never the
    registry's bare ``KeyError``, which would slip past the auto path's
    typed-error handling and crash the caller."""

    def test_unknown_transform_raises_typed_error_naming_the_column(self) -> None:
        # A typo'd transform on a 1:1 column. The error names the offending
        # column and the bad transform so the author can fix the rule.
        rule = _rule([
            {"column": "time", "source": {"role": "time"}, "transform": "yrealy"},
        ])
        with pytest.raises(UnknownTransformError, match="time") as exc:
            apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert exc.value.column == "time"
        assert exc.value.transform == "yrealy"

    def test_unknown_transform_is_an_estaterror_not_a_keyerror(self) -> None:
        # The contract that lets the auto path catch it as a typed error and
        # keeps a stray KeyError from leaking to the caller.
        rule = _rule([
            {"column": "value", "source": {"role": "value"}, "transform": "nope"},
        ])
        with pytest.raises(UnknownTransformError) as exc:
            apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert isinstance(exc.value, EstatError)
        assert isinstance(exc.value, RuleAuthoringError)
        assert not isinstance(exc.value, KeyError)

    def test_unknown_transform_on_a_pivot_meta_column_also_raises_typed(self) -> None:
        # The pivot path resolves transforms for its `where` columns through
        # the same guard, so a typo there is typed too (not a KeyError from a
        # different code path).
        rule = _pivot_rule([
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "amount", "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}}, "transform": "nope"},
        ])
        with pytest.raises(UnknownTransformError, match="amount"):
            apply_v2_rule(
                _PIVOT_ROWS, _PIVOT_CLASSIFICATION, rule, class_objs=_PIVOT_CLASS_OBJS,
            )


class TestRuleAuthoringErrorHierarchy:
    def test_apply_time_authoring_errors_share_one_base(self) -> None:
        # apply_auto catches RuleAuthoringError, so the surface/degrade policy
        # covers every leaf — and a future leaf (e.g. #4's standard-code
        # errors) — without editing the except clause. Pin the hierarchy that
        # guarantee rests on.
        assert issubclass(RoleResolutionError, RuleAuthoringError)
        assert issubclass(RuleExpansionError, RuleAuthoringError)
        assert issubclass(UnknownTransformError, RuleAuthoringError)


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


# A trade-like meta-axis table (#17 pattern 2): cat02 splits one logical
# (cat01, area, time) record into one row per measure. The member *names*
# (合計_金額 / 合計_数量2 / 単位2) carry the semantics a pivot rule selects on;
# the codes (140 / 130 / 110) are opaque and table-specific.
_PIVOT_CLASSIFICATION = TableClassification((
    _axis("cat01", AxisRole.CATEGORY),
    _axis("cat02", AxisRole.META_AXIS),
    _axis("area", AxisRole.AREA),
    _axis("time", AxisRole.TIME),
))
_PIVOT_CLASS_OBJS = (
    _classobj("cat02", [("110", "単位2"), ("130", "合計_数量2"), ("140", "合計_金額")]),
)
_PIVOT_ROWS = (
    # group A — all three measures present
    {"cat01": "0101", "cat02": "110", "area": "50103", "time": "2005000000", "value": "ＮＯ"},
    {"cat01": "0101", "cat02": "130", "area": "50103", "time": "2005000000", "value": "16"},
    {"cat01": "0101", "cat02": "140", "area": "50103", "time": "2005000000", "value": "35220"},
    # group B — the 単位 (unit) member is absent (the CPI-weight missing case)
    {"cat01": "0101", "cat02": "130", "area": "50104", "time": "2005000000", "value": "7"},
    {"cat01": "0101", "cat02": "140", "area": "50104", "time": "2005000000", "value": "99"},
)


def _pivot_rule(output: list[dict]) -> RuleV2:
    return RuleV2.model_validate({
        "schema_version": "2",
        "match": {"role_pattern": ["category", "meta-axis", "area", "time"]},
        "output": output,
    })


_TRADE_OUTPUT = [
    {"column": "cat01", "source": {"role": "category"}, "transform": "passthrough"},
    {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
    {"column": "unit", "source": {"role": "meta-axis", "where": {"equals": "単位2"}}},
    {"column": "quantity", "source": {"role": "meta-axis", "where": {"equals": "合計_数量2"}}},
    {"column": "amount_jpy", "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}}},
]


class TestApplyV2Pivot:
    """N:1 pivot (#10): a meta-axis ``where`` predicate folds rows spread
    across the meta-axis into one record per non-meta group."""

    def test_collapses_spread_rows_into_one_record_per_group(self) -> None:
        # The core Done: the three cat02 rows of group A become one row.
        # Non-meta columns (cat01/area/time) read the group's shared codes
        # (time normalized by its transform); each meta column selects its
        # member by *name* and surfaces that member's cell.
        out = apply_v2_rule(
            _PIVOT_ROWS, _PIVOT_CLASSIFICATION, _pivot_rule(_TRADE_OUTPUT),
            class_objs=_PIVOT_CLASS_OBJS,
        )
        assert out[0] == {
            "cat01": "0101", "area": "50103", "time": "2005",
            "unit": "ＮＯ", "quantity": "16", "amount_jpy": "35220",
        }

    def test_one_output_row_per_logical_record(self) -> None:
        # Two groups in, two rows out — the meta dimension is folded away,
        # not multiplied.
        out = apply_v2_rule(
            _PIVOT_ROWS, _PIVOT_CLASSIFICATION, _pivot_rule(_TRADE_OUTPUT),
            class_objs=_PIVOT_CLASS_OBJS,
        )
        assert len(out) == 2

    def test_missing_meta_member_yields_none_but_keeps_the_row(self) -> None:
        # Group B has no 単位 row (the CPI-weight-dropped case): the record
        # is still emitted with the rest of its measures, and the absent
        # column is None — a stable shape across table revisions.
        out = apply_v2_rule(
            _PIVOT_ROWS, _PIVOT_CLASSIFICATION, _pivot_rule(_TRADE_OUTPUT),
            class_objs=_PIVOT_CLASS_OBJS,
        )
        assert out[1] == {
            "cat01": "0101", "area": "50104", "time": "2005",
            "unit": None, "quantity": "7", "amount_jpy": "99",
        }

    def test_selects_member_by_nfkc_normalized_name(self) -> None:
        # The author may write the selector half-width while the metadata
        # carries it full-width (or vice versa); NFKC folding makes them
        # compare equal, so width drift does not silently drop a measure.
        class_objs = (_classobj("cat02", [("140", "合計_金額３")]),)  # full-width 3
        rows = (
            {"cat01": "0101", "cat02": "140", "area": "50103", "time": "2005000000", "value": "35220"},
        )
        rule = _pivot_rule([
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "amount", "source": {"role": "meta-axis", "where": {"equals": "合計_金額3"}}},  # half-width 3
        ])
        out = apply_v2_rule(rows, _PIVOT_CLASSIFICATION, rule, class_objs=class_objs)
        assert out[0]["amount"] == "35220"

    def test_meta_axis_source_without_where_fails_identifiably(self) -> None:
        # A meta-axis column with no where predicate cannot bind to a single
        # member; raise a typed error so the auto path routes to Layer D
        # rather than surfacing raw meta codes.
        rule = _pivot_rule([
            {"column": "x", "source": {"role": "meta-axis"}, "transform": "passthrough"},
        ])
        with pytest.raises(RoleResolutionError, match="where"):
            apply_v2_rule(
                _PIVOT_ROWS, _PIVOT_CLASSIFICATION, rule, class_objs=_PIVOT_CLASS_OBJS,
            )

    def test_pivot_without_class_metadata_fails_identifiably(self) -> None:
        # Matching `where` by member name needs the class metadata; a pivot
        # rule applied without it fails with a typed error (the auto path
        # always supplies class_objs, so this only bites bare callers).
        with pytest.raises(RoleResolutionError, match="metadata"):
            apply_v2_rule(_PIVOT_ROWS, _PIVOT_CLASSIFICATION, _pivot_rule(_TRADE_OUTPUT))

    def test_multiple_meta_axes_fails_identifiably(self) -> None:
        # A pivot needs a single meta-axis to fold around; two leaves the
        # `where` selector ambiguous (which axis's member?). Fail with a
        # typed error so the auto path routes to Layer D rather than folding
        # around an arbitrary one.
        two_meta = TableClassification((
            _axis("cat02", AxisRole.META_AXIS),
            _axis("cat03", AxisRole.META_AXIS),
            _axis("time", AxisRole.TIME),
        ))
        rule = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["meta-axis", "meta-axis", "time"]},
            "output": [
                {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "x", "source": {"role": "meta-axis", "where": {"equals": "数量"}}},
            ],
        })
        with pytest.raises(RoleResolutionError, match="one meta-axis"):
            apply_v2_rule(_PIVOT_ROWS, two_meta, rule, class_objs=_PIVOT_CLASS_OBJS)

    def test_group_key_spans_every_non_meta_axis_even_if_not_output(self) -> None:
        # The group key must include EVERY non-meta axis, not just the ones a
        # column emits. Here the rule omits the `area` column, yet two rows
        # differing only by area are distinct logical records and must not
        # fold together — otherwise one area's measure silently overwrites
        # the other's.
        class_objs = (_classobj("cat02", [("140", "合計_金額")]),)
        rows = (
            {"cat01": "X", "cat02": "140", "area": "50103", "time": "2005000000", "value": "100"},
            {"cat01": "X", "cat02": "140", "area": "50104", "time": "2005000000", "value": "200"},
        )
        rule = _pivot_rule([  # note: no `area` column declared
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "amount", "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}}},
        ])
        out = apply_v2_rule(rows, _PIVOT_CLASSIFICATION, rule, class_objs=class_objs)
        assert {row["amount"] for row in out} == {"100", "200"}

    def test_transform_runs_on_the_selected_meta_cell(self) -> None:
        # A non-passthrough transform on a `where` column must be applied to
        # the selected member's cell. A meta member whose cell is a date code
        # with the yearly transform proves the transform runs (not just
        # passthrough), distinguishing "transform applied" from "cell echoed".
        class_objs = (_classobj("cat02", [("900", "観測年")]),)
        rows = (
            {"cat01": "X", "cat02": "900", "area": "50103", "time": "2005000000", "value": "2020000000"},
        )
        rule = _pivot_rule([
            {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
            {"column": "obs_year", "source": {"role": "meta-axis", "where": {"equals": "観測年"}}, "transform": "yearly"},
        ])
        out = apply_v2_rule(rows, _PIVOT_CLASSIFICATION, rule, class_objs=class_objs)
        assert out[0]["obs_year"] == "2020"
