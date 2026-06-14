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
    TimeFormatError,
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
    def test_emits_declared_columns_as_canonical_cells(self) -> None:
        # The core Done (#35): long-form columns drive the output, each as a
        # canonical cell. time is a full time object (normalized structurally),
        # area is a {code,label} dimension (label == code with no metadata
        # here), and the value role is a {value,unit} measure reading the
        # observation cell — not an axis code.
        rule = _rule([
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
            {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
        ])
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert out == (
            {
                "time": {"code": "2020000000", "label": "2020000000",
                         "normalized": "2020", "granularity": "yearly"},
                "area": {"code": "13000", "label": "13000"},
                "value": {"value": "123", "unit": None},
            },
            {
                "time": {"code": "2021000000", "label": "2021000000",
                         "normalized": "2021", "granularity": "yearly"},
                "area": {"code": "27000", "label": "27000"},
                "value": {"value": "456", "unit": None},
            },
        )

    def test_value_role_reads_the_cell_not_the_tab_axis_code(self) -> None:
        # The VALUE role is special: its source is the observation cell
        # ("value"), even though the classifier assigns the role to the
        # single-member tab axis. A column drawing on it must surface 123,
        # not the tab code "020".
        rule = _rule([{"column": "v", "source": {"role": "value"}, "transform": "passthrough"}])
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert out[0] == {"v": {"value": "123", "unit": None}}


class TestApplyV2ShortForm:
    def test_accepts_short_form_by_expanding_defensively(self) -> None:
        # apply expands internally, so a caller (e.g. #28 building a Layer
        # A rule in memory) can pass a short-form rule without a separate
        # load step.
        rule = _rule([{"column": "time"}, {"column": "area"}, {"column": "value"}])
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert out[0] == {
            "time": {"code": "2020000000", "label": "2020000000",
                     "normalized": "2020", "granularity": "yearly"},
            "area": {"code": "13000", "label": "13000"},
            "value": {"value": "123", "unit": None},
        }


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


class TestTimeFormat:
    """A TIME column's *declared* format drives the time cell (#35). The
    total ``best_effort_time`` role-default keeps an unrecognised code raw; a
    declared *strict* format the data violates, or a non-time transform, is a
    typed :class:`TimeFormatError` — so a declared format is honored, not
    silently replaced by a best-effort guess. The auto path routes these by
    provenance (covered in test_endpoint_rules); here the apply layer raises
    directly."""

    def test_declared_strict_format_drives_the_cell(self) -> None:
        # quarterly_e_stat on a quarter-shaped code yields the quarterly object
        # — the declared format, not best_effort, governs normalized/granularity.
        rows = ({"time": "2020000103", "area": "13000", "tab": "020", "value": "1"},)
        rule = _rule([
            {"column": "time", "source": {"role": "time"}, "transform": "quarterly_e_stat"},
        ])
        out = apply_v2_rule(rows, _CLASSIFICATION, rule)
        assert out[0]["time"] == {
            "code": "2020000103", "label": "2020000103",
            "normalized": "2020-Q1", "granularity": "quarterly",
        }

    def test_strict_format_mismatching_the_code_raises_typed_error(self) -> None:
        # The author declared monthly, but _ROWS' codes are yearly-shaped
        # (2020000000). Rather than silently best-efforting to a yearly result,
        # the mismatch surfaces as a typed error naming the column and format.
        rule = _rule([
            {"column": "time", "source": {"role": "time"}, "transform": "monthly_e_stat"},
        ])
        with pytest.raises(TimeFormatError, match="time") as exc:
            apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        assert exc.value.column == "time"
        assert exc.value.transform == "monthly_e_stat"

    def test_non_time_transform_on_a_time_column_raises_typed_error(self) -> None:
        # passthrough is a valid transform but not a time format; a time column
        # must declare a time parser, so this is an authoring error (not a
        # silent passthrough of the raw code).
        rule = _rule([
            {"column": "time", "source": {"role": "time"}, "transform": "passthrough"},
        ])
        with pytest.raises(TimeFormatError, match="not a time format"):
            apply_v2_rule(_ROWS, _CLASSIFICATION, rule)

    def test_best_effort_reads_the_member_name_to_spot_a_year_span(self) -> None:
        # Population vital statistics (0003001309): the year-span code
        # 2006001010 is byte-identical to monthly 2006-10; only the member
        # name 「2006年10月～2007年9月」 tells them apart (#33). The
        # best_effort_time role-default — what every Layer A generic rule
        # declares — must consult it, not just the code.
        rows = ({"time": "2006001010", "area": "00000", "tab": "020", "value": "1"},)
        rule = _rule([
            {"column": "time", "source": {"role": "time"}, "transform": "best_effort_time"},
        ])
        objs = (_classobj("time", [("2006001010", "2006年10月～2007年9月")]),)
        out = apply_v2_rule(rows, _CLASSIFICATION, rule, class_objs=objs)
        assert out[0]["time"]["granularity"] == "yearly"
        assert out[0]["time"]["normalized"] == "2006-10"


class TestRuleAuthoringErrorHierarchy:
    def test_apply_time_authoring_errors_share_one_base(self) -> None:
        # apply_auto catches RuleAuthoringError, so the surface/degrade policy
        # covers every leaf — and a future leaf (e.g. #4's standard-code
        # errors) — without editing the except clause. Pin the hierarchy that
        # guarantee rests on.
        assert issubclass(RoleResolutionError, RuleAuthoringError)
        assert issubclass(RuleExpansionError, RuleAuthoringError)
        assert issubclass(UnknownTransformError, RuleAuthoringError)
        assert issubclass(TimeFormatError, RuleAuthoringError)


class TestLayerAGenericRuleNeverRaises:
    def test_role_default_only_rule_degrades_instead_of_raising(self) -> None:
        # The auto-path guarantee from the design discussion: a rule built
        # purely from role-defaults (what Layer A generates) applies
        # without raising even on adversarial codes — a time code that no
        # parser recognises is preserved raw, not thrown.
        rule = _rule([{"column": "time"}, {"column": "area"}, {"column": "value"}])
        adversarial = ({"time": "garbage", "area": "??", "tab": "020", "value": "-"},)
        out = apply_v2_rule(adversarial, _CLASSIFICATION, rule)
        # An unrecognised time code keeps normalized == code (granularity
        # None); area passes through; the value cell is preserved raw.
        assert out == ({
            "time": {"code": "garbage", "label": "garbage",
                     "normalized": "garbage", "granularity": None},
            "area": {"code": "??", "label": "??"},
            "value": {"value": "-", "unit": None},
        },)


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
        # Non-meta columns (cat01/area/time) read the group's shared codes as
        # canonical cells (time normalized structurally); each meta column
        # selects its member by *name* and surfaces that member's cell as a
        # {value,unit} measure (no unit on these rows → unit None).
        out = apply_v2_rule(
            _PIVOT_ROWS, _PIVOT_CLASSIFICATION, _pivot_rule(_TRADE_OUTPUT),
            class_objs=_PIVOT_CLASS_OBJS,
        )
        assert out[0] == {
            "cat01": {"code": "0101", "label": "0101"},
            "area": {"code": "50103", "label": "50103"},
            "time": {"code": "2005000000", "label": "2005000000",
                     "normalized": "2005", "granularity": "yearly"},
            "unit": {"value": "ＮＯ", "unit": None},
            "quantity": {"value": "16", "unit": None},
            "amount_jpy": {"value": "35220", "unit": None},
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
            "cat01": {"code": "0101", "label": "0101"},
            "area": {"code": "50104", "label": "50104"},
            "time": {"code": "2005000000", "label": "2005000000",
                     "normalized": "2005", "granularity": "yearly"},
            "unit": None,
            "quantity": {"value": "7", "unit": None},
            "amount_jpy": {"value": "99", "unit": None},
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
        assert out[0]["amount"]["value"] == "35220"

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
        assert {row["amount"]["value"] for row in out} == {"100", "200"}

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
        assert out[0]["obs_year"]["value"] == "2020"

    def test_pivot_measures_keep_their_own_units(self) -> None:
        # Trade's defining case (#35 decision 2): quantity is counted in ＮＯ,
        # amount in 千円. Each pivoted measure column carries *its member's
        # own* unit — a single shared unit sibling could not represent two
        # measures with different units.
        class_objs = (_classobj("cat02", [("130", "合計_数量2"), ("140", "合計_金額")]),)
        rows = (
            {"cat01": "0101", "cat02": "130", "area": "50103",
             "time": "2005000000", "value": "16", "unit": "ＮＯ"},
            {"cat01": "0101", "cat02": "140", "area": "50103",
             "time": "2005000000", "value": "35220", "unit": "千円"},
        )
        rule = _pivot_rule([
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "quantity", "source": {"role": "meta-axis", "where": {"equals": "合計_数量2"}}},
            {"column": "amount", "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}}},
        ])
        out = apply_v2_rule(rows, _PIVOT_CLASSIFICATION, rule, class_objs=class_objs)
        assert out[0]["quantity"] == {"value": "16", "unit": "ＮＯ"}
        assert out[0]["amount"] == {"value": "35220", "unit": "千円"}


# A trade measure×period cross (#37): cat02 folds *two* dimensions into one
# axis — a measure family (合計_数量2 / 合計_金額, level 1) and the month
# (level 2, linked to its family by @parentCode). The month identity lives
# only in the member *name* ("1月_金額"), not in any code, so a rule derives
# it with a `key` pattern and selects the measure family with a `where`
# parent predicate — neither expressible by name-equality alone.
def _hier_classobj(axis_id: str, members: list[dict]) -> ClassObj:
    """A ClassObj from full CLASS dicts (code/name/level/parentCode/unit),
    so a pivot rule can match on parent and depth, not just the name."""
    return ClassObj(id=axis_id, name=axis_id, classes=tuple(members))


_CROSS_CLASSIFICATION = TableClassification((
    _axis("cat01", AxisRole.CATEGORY),
    _axis("cat02", AxisRole.META_AXIS),
    _axis("area", AxisRole.AREA),
    _axis("time", AxisRole.TIME),
))
_CROSS_CLASS_OBJS = (
    _hier_classobj("cat02", [
        {"code": "130", "name": "合計_数量2", "level": "1"},
        {"code": "140", "name": "合計_金額", "level": "1", "unit": "千円"},
        {"code": "160", "name": "1月_数量2", "level": "2", "parentCode": "130"},
        {"code": "170", "name": "1月_金額", "level": "2", "parentCode": "140", "unit": "千円"},
        {"code": "190", "name": "2月_数量2", "level": "2", "parentCode": "130"},
        {"code": "200", "name": "2月_金額", "level": "2", "parentCode": "140", "unit": "千円"},
    ]),
)
# One logical record (cat01=0101, area=50103, time=2026) spread across the six
# level-2 members; the two level-1 totals are present too (a real table ships
# both), to prove they do not leak into the per-month rows.
_CROSS_ROWS = (
    {"cat01": "0101", "cat02": "130", "area": "50103", "time": "2026000000", "value": "13"},
    {"cat01": "0101", "cat02": "140", "area": "50103", "time": "2026000000", "value": "76300", "unit": "千円"},
    {"cat01": "0101", "cat02": "160", "area": "50103", "time": "2026000000", "value": "6"},
    {"cat01": "0101", "cat02": "170", "area": "50103", "time": "2026000000", "value": "35220", "unit": "千円"},
    {"cat01": "0101", "cat02": "190", "area": "50103", "time": "2026000000", "value": "7"},
    {"cat01": "0101", "cat02": "200", "area": "50103", "time": "2026000000", "value": "41080", "unit": "千円"},
)


def _cross_rule(output: list[dict]) -> RuleV2:
    return RuleV2.model_validate({
        "schema_version": "2",
        "match": {"role_pattern": ["category", "meta-axis", "area", "time"]},
        "output": output,
    })


class TestApplyV2DerivedGrain:
    """``key`` derives a grain dimension from member names (#37): the
    measure×period cross folds into one row per (group, derived key), with
    `where` selecting each measure within that row — no member enumeration."""

    def test_month_rows_by_measure_columns(self) -> None:
        # The core Done: 6 spread rows → 2 month rows, each carrying the
        # quantity and amount for that month. `key` puts the month (read from
        # the member name) into the grain; `where` parent picks the measure
        # family. No member name is enumerated in the rule.
        rule = _cross_rule([
            {"column": "commodity", "source": {"role": "category"}, "transform": "passthrough"},
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "month",
             "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            {"column": "quantity",
             "source": {"role": "meta-axis", "where": {"parent": "合計_数量2"}}},
            {"column": "amount",
             "source": {"role": "meta-axis", "where": {"parent": "合計_金額"}}},
        ])
        out = apply_v2_rule(_CROSS_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_CROSS_CLASS_OBJS)
        assert out == (
            {
                "commodity": {"code": "0101", "label": "0101"},
                "time": {"code": "2026000000", "label": "2026000000",
                         "normalized": "2026", "granularity": "yearly"},
                "month": "1月",
                "quantity": {"value": "6", "unit": None},
                "amount": {"value": "35220", "unit": "千円"},
            },
            {
                "commodity": {"code": "0101", "label": "0101"},
                "time": {"code": "2026000000", "label": "2026000000",
                         "normalized": "2026", "granularity": "yearly"},
                "month": "2月",
                "quantity": {"value": "7", "unit": None},
                "amount": {"value": "41080", "unit": "千円"},
            },
        )

    def test_where_parent_selects_one_member_within_the_grain(self) -> None:
        # Across the group, parent=合計_金額 matches both 1月_金額 and 2月_金額;
        # the grain (month) narrows it to exactly one per row. This is what
        # makes a multi-member parent predicate unambiguous.
        rule = _cross_rule([
            {"column": "month",
             "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            {"column": "amount",
             "source": {"role": "meta-axis", "where": {"parent": "合計_金額"}}},
        ])
        out = apply_v2_rule(_CROSS_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_CROSS_CLASS_OBJS)
        assert {r["month"]: r["amount"]["value"] for r in out} == {"1月": "35220", "2月": "41080"}

    def test_where_matching_several_members_without_a_key_raises(self) -> None:
        # `where: {level: "2"}` matches all six level-2 members of the group;
        # with no key to split them into rows there is no single cell to
        # surface, so fail loud (the author must add a key) rather than pick
        # an arbitrary member.
        rule = _cross_rule([
            {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
            {"column": "x", "source": {"role": "meta-axis", "where": {"level": "2"}}},
        ])
        with pytest.raises(RoleResolutionError, match="matched"):
            apply_v2_rule(_CROSS_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_CROSS_CLASS_OBJS)

    def test_where_matching_several_members_within_one_grain_raises(self) -> None:
        # Even with a key, a predicate that stays ambiguous *inside* a grain
        # cell (level "2" still matches 数量2 and 金額 for one month) fails —
        # the grain must resolve the where to exactly one member.
        rule = _cross_rule([
            {"column": "month",
             "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            {"column": "x", "source": {"role": "meta-axis", "where": {"level": "2"}}},
        ])
        with pytest.raises(RoleResolutionError, match="matched"):
            apply_v2_rule(_CROSS_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_CROSS_CLASS_OBJS)

    def test_key_pattern_without_a_capture_group_uses_the_whole_match(self) -> None:
        # The grain value is the first capture group, or — when the pattern
        # declares none — the whole match. An author may write the simpler
        # group-less form and still get a usable key.
        rule = _cross_rule([
            {"column": "month", "source": {"role": "meta-axis", "key": {"pattern": r"\d{1,2}月"}}},
            {"column": "amount", "source": {"role": "meta-axis", "where": {"parent": "合計_金額"}}},
        ])
        out = apply_v2_rule(_CROSS_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_CROSS_CLASS_OBJS)
        assert {r["month"] for r in out} == {"1月", "2月"}

    def test_duplicate_rows_of_one_member_in_a_group_take_the_first(self) -> None:
        # A member duplicated within one group (a malformed response, or an
        # axis outside the role pattern) must not be mistaken for the #37
        # ambiguity of *several* members matching one predicate: it collapses
        # to its first row (the pre-#37 graceful behavior), not an error.
        class_objs = (_classobj("cat02", [("140", "合計_金額")]),)
        rows = (
            {"cat01": "X", "cat02": "140", "area": "50103", "time": "2005000000", "value": "100"},
            {"cat01": "X", "cat02": "140", "area": "50103", "time": "2005000000", "value": "999"},
        )
        rule = _pivot_rule([
            {"column": "amount", "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}}},
        ])
        out = apply_v2_rule(rows, _PIVOT_CLASSIFICATION, rule, class_objs=class_objs)
        assert len(out) == 1
        assert out[0]["amount"]["value"] == "100"

    def test_level_and_parent_combine_as_and(self) -> None:
        # Several selectors on one `where` narrow together: level "1" AND
        # parent ... no — a level-1 member has no parent. Use level "2" AND
        # parent 合計_金額 to confirm the AND reaches the same single member
        # the parent alone would (the level clause does not exclude it).
        rule = _cross_rule([
            {"column": "month",
             "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            {"column": "amount",
             "source": {"role": "meta-axis", "where": {"parent": "合計_金額", "level": "2"}}},
        ])
        out = apply_v2_rule(_CROSS_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_CROSS_CLASS_OBJS)
        assert {r["month"]: r["amount"]["value"] for r in out} == {"1月": "35220", "2月": "41080"}


# A trade quantity's unit (#39): e-Stat ships the unit not as an `@unit` but as
# a level-1 member (単位2) whose *observation value* is the unit string ("ＮＯ"
# = count). That member carries no period grain, so #37's `key` drops it from
# every month row. A `unit_from` predicate on the measure column reaches the
# grain-less member and folds its value into the measure's `unit`, so a quantity
# cell is self-describing ({value, unit}) like every other measure — not a
# sibling column the consumer must pair by hand.
_UNIT_CLASS_OBJS = (
    _hier_classobj("cat02", [
        {"code": "110", "name": "単位2", "level": "1"},
        {"code": "130", "name": "合計_数量2", "level": "1"},
        {"code": "140", "name": "合計_金額", "level": "1", "unit": "千円"},
        {"code": "160", "name": "1月_数量2", "level": "2", "parentCode": "130"},
        {"code": "170", "name": "1月_金額", "level": "2", "parentCode": "140", "unit": "千円"},
        {"code": "190", "name": "2月_数量2", "level": "2", "parentCode": "130"},
        {"code": "200", "name": "2月_金額", "level": "2", "parentCode": "140", "unit": "千円"},
    ]),
)
# The 単位2 row's *value* is the unit string; the year totals (合計_*) ride
# along as a real table ships them — none of these grain-less level-1 members
# forms a month row, but 単位2's value reaches each month row as the broadcast
# unit.
_UNIT_ROWS = (
    {"cat01": "0101", "cat02": "110", "area": "50103", "time": "2026000000", "value": "ＮＯ"},
    {"cat01": "0101", "cat02": "130", "area": "50103", "time": "2026000000", "value": "13"},
    {"cat01": "0101", "cat02": "140", "area": "50103", "time": "2026000000", "value": "76300", "unit": "千円"},
    {"cat01": "0101", "cat02": "160", "area": "50103", "time": "2026000000", "value": "6"},
    {"cat01": "0101", "cat02": "170", "area": "50103", "time": "2026000000", "value": "35220", "unit": "千円"},
    {"cat01": "0101", "cat02": "190", "area": "50103", "time": "2026000000", "value": "7"},
    {"cat01": "0101", "cat02": "200", "area": "50103", "time": "2026000000", "value": "41080", "unit": "千円"},
)


class TestApplyV2UnitBroadcast:
    """`unit_from` folds a grain-less unit member's value into a measure's unit
    (#39): trade quantities come back self-describing, the unit broadcast to
    every period row of the group."""

    def test_quantity_carries_its_broadcast_unit(self) -> None:
        # The core Done: each month's quantity cell carries the unit ("ＮＯ")
        # read from the grain-less 単位2 member. The amount column, with no
        # unit_from, is untouched — it keeps its own @unit (千円). No sibling
        # unit column: the measure is self-describing like every other measure.
        rule = _cross_rule([
            {"column": "month",
             "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            {"column": "quantity",
             "source": {"role": "meta-axis", "where": {"parent": "合計_数量2"},
                        "unit_from": {"equals": "単位2"}}},
            {"column": "amount",
             "source": {"role": "meta-axis", "where": {"parent": "合計_金額"}}},
        ])
        out = apply_v2_rule(_UNIT_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_UNIT_CLASS_OBJS)
        assert {r["month"]: r["quantity"] for r in out} == {
            "1月": {"value": "6", "unit": "ＮＯ"},
            "2月": {"value": "7", "unit": "ＮＯ"},
        }
        assert {r["month"]: r["amount"]["unit"] for r in out} == {"1月": "千円", "2月": "千円"}

    def test_unit_from_matching_several_members_raises(self) -> None:
        # `unit_from: {level: "1"}` matches three distinct level-1 members
        # (単位2 / 合計_数量2 / 合計_金額); there is no single unit to fold, so
        # fail loud like an ambiguous `where` — the author must narrow it.
        rule = _cross_rule([
            {"column": "month",
             "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            {"column": "quantity",
             "source": {"role": "meta-axis", "where": {"parent": "合計_数量2"},
                        "unit_from": {"level": "1"}}},
        ])
        with pytest.raises(RoleResolutionError, match="unit_from"):
            apply_v2_rule(_UNIT_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_UNIT_CLASS_OBJS)

    def test_unit_from_matching_no_member_leaves_unit_none(self) -> None:
        # A unit_from that matches nothing (a retired or absent unit member)
        # leaves the measure's unit None rather than dropping the cell — the
        # same graceful stance #37 takes for a `where` matching no member.
        rule = _cross_rule([
            {"column": "month",
             "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            {"column": "quantity",
             "source": {"role": "meta-axis", "where": {"parent": "合計_数量2"},
                        "unit_from": {"equals": "単位9"}}},
        ])
        out = apply_v2_rule(_UNIT_ROWS, _CROSS_CLASSIFICATION, rule, class_objs=_UNIT_CLASS_OBJS)
        assert {r["month"]: r["quantity"]["unit"] for r in out} == {"1月": None, "2月": None}
