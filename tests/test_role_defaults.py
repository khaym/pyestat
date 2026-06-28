"""Tests for the role-default registry and v2 transform registry.

This is the Layer A substance: the named transforms a v2 rule may
reference, and the per-role defaults that fill a short-form rule's gaps.
The hard requirement carried over from the design discussion is that
the *defaults* are total functions — a rule built purely from them (a
Layer A generic rule) can never raise at apply time, so the auto path
always has output to return.
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
from pyestat._engine.role_defaults import (
    TRANSFORMS,
    build_generic_rule,
    default_transform,
)
from pyestat._errors import RoleResolutionError


class TestTransformRegistry:
    def test_minimum_registered_transforms(self) -> None:
        # Done scopes the registry to "passthrough + the existing time
        # parsers" adds iso8601 / jis_x_0401 later. Pinning the floor
        # so that expansion does not depend on transforms not yet shipped.
        names = set(TRANSFORMS.names())
        assert {"passthrough", "monthly_e_stat", "quarterly_e_stat", "yearly",
                "best_effort_time"} <= names

    def test_passthrough_returns_input_unchanged(self) -> None:
        assert TRANSFORMS.resolve("passthrough")("ＮＯ") == "ＮＯ"

    def test_named_time_parser_emits_normalized_string(self) -> None:
        # Decision 3: a v2 time column yields the normalized string only
        # (the v1 companion _code / _granularity columns are not emitted).
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


def _classobj(axis_id: str, members: list[tuple[str, str]]) -> ClassObj:
    """A ClassObj from (code, name) pairs — the meta-axis member names a
    generated pivot rule turns into one ``where`` column each. Flat (no
    ``@level``/``@parentCode``), so it stands for the clean 表章項目 axis."""
    return ClassObj(
        id=axis_id,
        name=axis_id,
        classes=tuple({"code": code, "name": name} for code, name in members),
    )


def _classobj_hier(axis_id: str, members: list[tuple[str, str, str, str | None]]) -> ClassObj:
    """A ClassObj whose members carry a code hierarchy. Each member is
    ``(code, name, level, parentCode)`` (parentCode ``None`` for a root) — the
    shape e-Stat uses for a cross axis like trade's 合計/月次 × 数量/金額."""
    return ClassObj(
        id=axis_id,
        name=axis_id,
        classes=tuple(
            {"code": c, "name": n, "level": lv, **({"parentCode": p} if p else {})}
            for c, n, lv, p in members
        ),
    )


class TestBuildGenericRule:
    """``build_generic_rule`` turns a classification into a Layer A rule, or
    declines (``None``) when the table cannot be structured generically and
    must route to Layer D.

    Business rule: Layer A structures any table whose axes it can address. A
    role that repeats across axes (建築主 × 用途) is no longer a blocker —
    each axis gets its own id-addressed column. A meta-axis is handled by the
    pivot path (see TestBuildGenericPivot); only an ``unknown`` axis (the
    classifier's route-to-D sentinel) or a column-name collision still makes a
    table ineligible, so it rides Layer D instead.
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
        # The built rule applies directly, emitting canonical cells:
        # a time object and a {value,unit} measure (no unit on this row).
        assert apply_v2_rule(rows, clf, rule) == ({
            "time": {"code": "2020000000", "label": "2020000000",
                     "normalized": "2020", "granularity": "yearly"},
            "value": {"value": "126146", "unit": None},
        },)

    def test_meta_axis_declines_without_class_objs(self) -> None:
        # A meta-axis can be auto-pivoted (see TestBuildGenericPivot), but only
        # when the member names are available to name the where-columns. With
        # no class metadata the names are unknown, so route to Layer D.
        clf = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("area", AxisRole.AREA),
        ))
        assert build_generic_rule(clf) is None

    def test_unknown_axis_declines(self) -> None:
        clf = TableClassification((
            _axis("cat01", AxisRole.UNKNOWN),
            _axis("tab", AxisRole.VALUE),
        ))
        assert build_generic_rule(clf) is None

    def test_repeated_role_yields_one_axis_addressed_column_each(self) -> None:
        # 賃金 (職種 × 企業規模) and the like carry two category axes. Each
        # becomes its own column, addressed by axis id — the value column reads
        # the observation, the rest read their own axis. (Earlier this whole
        # table declined to Layer D for want of a way to tell the two apart.)
        clf = TableClassification((
            _axis("time", AxisRole.TIME),
            _axis("cat01", AxisRole.CATEGORY),
            _axis("cat03", AxisRole.CATEGORY),
            _axis("tab", AxisRole.VALUE),
        ))
        rule = build_generic_rule(clf)
        assert rule is not None
        cols = {c.column: c for c in rule.output}
        assert set(cols) == {"time", "cat01", "cat03", "value"}
        # The two same-role categories are disambiguated by axis id, so neither
        # column reads the other's axis.
        assert cols["cat01"].source.role == AxisRole.CATEGORY
        assert cols["cat01"].source.axis == "cat01"
        assert cols["cat03"].source.role == AxisRole.CATEGORY
        assert cols["cat03"].source.axis == "cat03"
        assert cols["value"].source.role == AxisRole.VALUE

    def test_multi_category_rule_maps_each_category_to_its_own_column(self) -> None:
        # Applied, the two categories land in distinct cells (not one
        # overwriting the other) — the observable 1:1 result for a multi-axis
        # wage-style table.
        clf = TableClassification((
            _axis("cat01", AxisRole.CATEGORY),
            _axis("cat03", AxisRole.CATEGORY),
            _axis("tab", AxisRole.VALUE),
            _axis("time", AxisRole.TIME),
        ))
        rule = build_generic_rule(clf)
        assert rule is not None
        objs = (
            _classobj("cat01", [("01", "管理職")]),
            _classobj("cat03", [("L", "大企業")]),
        )
        rows = ({"cat01": "01", "cat03": "L", "tab": "020",
                 "time": "2020000000", "value": "550", "unit": "千円"},)
        out = apply_v2_rule(rows, clf, rule, class_objs=objs)
        assert out[0]["cat01"] == {"code": "01", "label": "管理職"}
        assert out[0]["cat03"] == {"code": "L", "label": "大企業"}
        assert out[0]["value"] == {"value": "550", "unit": "千円"}

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

    def test_table_without_value_axis_still_emits_the_observation(self) -> None:
        # Population 0000150007 (category, area, time — no tab axis): the
        # observation cell exists on every e-Stat row regardless of whether a
        # tab axis describes it, so the generic rule must always declare a
        # "value" column. Earlier it was declared only when an axis carried
        # the VALUE role, and 124k observations silently vanished.
        clf = TableClassification((
            _axis("cat01", AxisRole.CATEGORY),
            _axis("area", AxisRole.AREA),
            _axis("time", AxisRole.TIME),
        ))
        rule = build_generic_rule(clf)
        assert rule is not None
        # The match pattern is untouched — the appended column does not leak
        # a VALUE role into rule resolution.
        assert list(rule.match.role_pattern) == [
            AxisRole.CATEGORY, AxisRole.AREA, AxisRole.TIME,
        ]
        cols = {c.column: c for c in rule.output}
        assert set(cols) == {"cat01", "area", "time", "value"}
        assert cols["value"].source.role == AxisRole.VALUE
        assert cols["value"].transform == "passthrough"
        rows = ({"cat01": "001", "area": "00000", "time": "1991000000",
                 "value": "124043", "unit": "人"},)
        out = apply_v2_rule(rows, clf, rule)
        assert out[0]["value"] == {"value": "124043", "unit": "人"}

    def test_axis_idd_value_without_value_role_declines(self) -> None:
        # Same collision as above, but on the appended observation column:
        # an axis literally id'd "value" (no VALUE role anywhere) would
        # collide with it, so decline (→ Layer D, which preserves both).
        clf = TableClassification((
            _axis("value", AxisRole.CATEGORY),
            _axis("time", AxisRole.TIME),
        ))
        assert build_generic_rule(clf) is None

    def test_generic_output_keeps_unit_and_labels(self) -> None:
        # GDP 0003364993 (value, category, time): the canonical cells
        # carry the row's unit (10億円) and the category's display label.
        # Regression guard — the earlier generic path dropped both,
        # leaving raw codes that an LLM agent cannot interpret.
        clf = TableClassification((
            _axis("tab", AxisRole.VALUE),
            _axis("cat01", AxisRole.CATEGORY),
            _axis("time", AxisRole.TIME),
        ))
        rule = build_generic_rule(clf)
        assert rule is not None
        rows = ({"tab": "10", "cat01": "11", "time": "1995100000",
                 "value": "18747.1", "unit": "10億円"},)
        objs = (_classobj("cat01", [("11", "1.国内FISIM産出額")]),)
        out = apply_v2_rule(rows, clf, rule, class_objs=objs)
        assert out[0]["value"] == {"value": "18747.1", "unit": "10億円"}
        assert out[0]["cat01"] == {"code": "11", "label": "1.国内FISIM産出額"}
        # The fiscal-year wire shape resolves as the April-start span.
        assert out[0]["time"]["normalized"] == "1995-04"
        assert out[0]["time"]["granularity"] == "yearly"


# A trade-like table (pattern 2): cat02 is the meta-axis whose members
# (単位2 / 合計_数量2 / 合計_金額) each spread one logical (cat01, area, time)
# record across rows. Auto-pivoting folds them back into one record.
_TRADE_CLF = TableClassification((
    _axis("cat01", AxisRole.CATEGORY),
    _axis("cat02", AxisRole.META_AXIS),
    _axis("area", AxisRole.AREA),
    _axis("time", AxisRole.TIME),
))
_TRADE_META = (
    _classobj("cat02", [("110", "単位2"), ("130", "合計_数量2"), ("140", "合計_金額")]),
)


class TestBuildGenericPivot:
    """Business rule: a table with exactly one meta-axis is no longer
    declined — Layer A auto-generates a *pivot* rule that folds the meta-axis
    members into one record per non-meta group, so an uncovered meta-axis
    table comes back folded (1 row per logical record) rather than spread.
    The meta-axis member names become the pivot's columns; the non-meta axes
    stay 1:1, each addressed by its axis id so a repeated non-meta role (建築主 ×
    用途) folds rather than declining. The decline conditions that would
    risk a wrong or raising rule (≥2 meta-axes, a column-name collision, or
    missing member names) still route to Layer D.
    """

    def test_single_meta_axis_yields_pivot_rule(self) -> None:
        rule = build_generic_rule(_TRADE_CLF, _TRADE_META)
        assert rule is not None
        cols = {c.column: c for c in rule.output}
        # Non-meta axes stay 1:1, in axis order, with their role-defaults.
        assert cols["cat01"].source.role == AxisRole.CATEGORY
        assert cols["cat01"].source.where is None
        assert cols["area"].source.role == AxisRole.AREA
        assert cols["time"].transform == "best_effort_time"
        # One where-column per meta member, named by its (NFKC) member name.
        for name in ("単位2", "合計_数量2", "合計_金額"):
            assert cols[name].source.role == AxisRole.META_AXIS
            assert cols[name].source.where.equals == name
            assert cols[name].transform == "passthrough"

    def test_built_pivot_rule_folds_rows_directly(self) -> None:
        # What Layer A builds applies directly (no separate load/expand) and
        # folds the three meta rows of a group into one record (canonical cells).
        rule = build_generic_rule(_TRADE_CLF, _TRADE_META)
        assert rule is not None
        rows = (
            {"cat01": "0101", "cat02": "110", "area": "50103", "time": "2005000000", "value": "ＮＯ"},
            {"cat01": "0101", "cat02": "130", "area": "50103", "time": "2005000000", "value": "16"},
            {"cat01": "0101", "cat02": "140", "area": "50103", "time": "2005000000", "value": "35220"},
        )
        out = apply_v2_rule(rows, _TRADE_CLF, rule, class_objs=_TRADE_META)
        assert len(out) == 1
        assert out[0]["cat01"] == {"code": "0101", "label": "0101"}
        assert out[0]["time"]["normalized"] == "2005"
        assert out[0]["単位2"] == {"value": "ＮＯ", "unit": None}
        assert out[0]["合計_数量2"] == {"value": "16", "unit": None}
        assert out[0]["合計_金額"] == {"value": "35220", "unit": None}

    def test_pivot_rule_emits_no_bare_value_column(self) -> None:
        # On the pivot shape the observation lives in each member's
        # where-column; a bare "value" column would read an arbitrary group
        # representative's cell. The observation column is appended only
        # on the 1:1 (no-meta) shape.
        rule = build_generic_rule(_TRADE_CLF, _TRADE_META)
        assert rule is not None
        assert "value" not in {c.column for c in rule.output}

    def test_member_name_is_nfkc_normalized(self) -> None:
        # The column/selector name folds full-width to half-width so it matches
        # the meta-member name the pivot path also NFKC-folds when selecting.
        objs = (_classobj("cat02", [("110", "金額２")]),)  # full-width 2
        rule = build_generic_rule(_TRADE_CLF, objs)
        assert rule is not None
        col = next(c for c in rule.output if c.source.where is not None)
        assert col.column == "金額2"
        assert col.source.where.equals == "金額2"

    def test_two_meta_axes_decline(self) -> None:
        # Two measure-spread axes need explicit disambiguation; Layer A only
        # folds a single meta-axis, so route to Layer D.
        clf = TableClassification((
            _axis("cat01", AxisRole.META_AXIS),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("time", AxisRole.TIME),
        ))
        objs = (_classobj("cat01", [("1", "a")]), _classobj("cat02", [("1", "b")]))
        assert build_generic_rule(clf, objs) is None

    def test_repeated_nonmeta_role_with_meta_axis_pivots(self) -> None:
        # 建築着工 (建築主 × 用途 + 測定量 meta + time). Two category axes
        # alongside the meta-axis no longer decline — each is addressed by its
        # axis id and grouped, while the meta-axis folds into where-columns.
        clf = TableClassification((
            _axis("cat01", AxisRole.CATEGORY),
            _axis("cat03", AxisRole.CATEGORY),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("time", AxisRole.TIME),
        ))
        rule = build_generic_rule(clf, _TRADE_META)
        assert rule is not None
        cols = {c.column: c for c in rule.output}
        # Both categories stay 1:1, disambiguated by axis id.
        assert cols["cat01"].source.axis == "cat01"
        assert cols["cat01"].source.where is None
        assert cols["cat03"].source.axis == "cat03"
        # The meta-axis still folds into one where-column per member.
        assert cols["合計_金額"].source.role == AxisRole.META_AXIS
        assert cols["合計_金額"].source.where.equals == "合計_金額"

    def test_building_starts_folds_measures_into_columns(self) -> None:
        # The headline case (0003114490): 測定量 (tab) is the meta-axis over
        # three measures; 建築主 (cat01) and 用途 (cat03) are two category axes,
        # plus area and time. Auto must group by (建築主, 用途, area, time) and
        # fold the three measures into one record — the shape that earlier
        # fell to Layer D, leaving the table spread one row per measure.
        clf = TableClassification((
            _axis("tab", AxisRole.META_AXIS),
            _axis("cat01", AxisRole.CATEGORY),
            _axis("cat03", AxisRole.CATEGORY),
            _axis("area", AxisRole.AREA),
            _axis("time", AxisRole.TIME),
        ))
        objs = (
            _classobj("tab", [("100", "建築物の数"), ("200", "床面積"), ("300", "工事費予定額")]),
            _classobj("cat01", [("P", "公共")]),
            _classobj("cat03", [("1", "居住用"), ("2", "非居住用")]),
        )
        rule = build_generic_rule(clf, objs)
        assert rule is not None
        # Two 用途 values × three measures: the grain spans *both* category axes,
        # so the six rows fold into two records (one per 用途), not one.
        rows = tuple(
            {"tab": tab, "cat01": "P", "cat03": use, "area": "13000",
             "time": "2020000000", "value": val}
            for use, measures in (("1", ("12", "3400", "56000")), ("2", ("4", "900", "21000")))
            for tab, val in zip(("100", "200", "300"), measures)
        )
        out = apply_v2_rule(rows, clf, rule, class_objs=objs)
        assert len(out) == 2  # grouped by (建築主, 用途, area, time), measures folded in
        by_use = {row["cat03"]["label"]: row for row in out}
        assert by_use["居住用"]["cat01"] == {"code": "P", "label": "公共"}
        assert by_use["居住用"]["area"] == {"code": "13000", "label": "13000"}
        assert by_use["居住用"]["建築物の数"] == {"value": "12", "unit": None}
        assert by_use["居住用"]["床面積"] == {"value": "3400", "unit": None}
        assert by_use["居住用"]["工事費予定額"] == {"value": "56000", "unit": None}
        # The second 用途 is a distinct record — proof the repeated-role axis
        # participates in the grain, not just the first category.
        assert by_use["非居住用"]["建築物の数"] == {"value": "4", "unit": None}
        assert by_use["非居住用"]["工事費予定額"] == {"value": "21000", "unit": None}

    def test_member_name_colliding_with_nonmeta_column_declines(self) -> None:
        # A meta member literally named "area" would collide with the area
        # column. Decline rather than let RuleV2's duplicate-column check raise
        # on the auto path (which would break the "auto never raises" promise).
        objs = (_classobj("cat02", [("110", "area"), ("140", "合計_金額")]),)
        assert build_generic_rule(_TRADE_CLF, objs) is None

    def test_duplicate_member_names_coalesce_into_one_column(self) -> None:
        # 賃金構造 "DB" tables: the meta-axis carries each measure twice —
        # codes 01/02 and 33/34 share names+units. The pair is a code-scheme
        # vintage, not a second dimension, so rather than declining the whole
        # table Layer A folds same-named members into ONE column per distinct
        # name (first-seen order).
        objs = (_classobj("cat02", [("01", "年齢"), ("02", "勤続年数"),
                                    ("33", "年齢"), ("34", "勤続年数")]),)
        rule = build_generic_rule(_TRADE_CLF, objs)
        assert rule is not None
        where_cols = [c.column for c in rule.output if c.source.where is not None]
        assert where_cols == ["年齢", "勤続年数"]

    def test_duplicate_members_coalesce_after_nfkc_fold(self) -> None:
        # The duplicate-name fold keys on the NFKC-normalized name (the same fold
        # the classifier and the pivot selector use), so two members whose names
        # differ only by full/half-width (金額２ / 金額2) are one measure and
        # collapse to a single column — they do not emit two same-named columns
        # that would trip RuleV2's duplicate-column validator.
        objs = (_classobj("cat02", [("11", "金額２"), ("22", "金額2")]),)
        rule = build_generic_rule(_TRADE_CLF, objs)
        assert rule is not None
        where_cols = [c.column for c in rule.output if c.source.where is not None]
        assert where_cols == ["金額2"]

    def test_duplicate_members_fold_when_each_cell_uses_one_block(self) -> None:
        # The common case: each group cell populates only one block — the code
        # vintage in effect that year. The 年齢 column reads whichever block's
        # member is present, so the table folds to one record per group with
        # the measure under the shared name (no per-vintage column).
        objs = (_classobj("cat02", [("01", "年齢"), ("33", "年齢")]),)
        rule = build_generic_rule(_TRADE_CLF, objs)
        assert rule is not None
        rows = (
            {"cat01": "A", "cat02": "01", "area": "00000", "time": "2021000000",
             "value": "50.6", "unit": "歳"},  # group A: old-code block only
            {"cat01": "B", "cat02": "33", "area": "00000", "time": "2023000000",
             "value": "51.0", "unit": "歳"},  # group B: new-code block only
        )
        out = apply_v2_rule(rows, _TRADE_CLF, rule, class_objs=objs)
        by_cat = {r["cat01"]["code"]: r for r in out}
        assert by_cat["A"]["年齢"] == {"value": "50.6", "unit": "歳"}
        assert by_cat["B"]["年齢"] == {"value": "51.0", "unit": "歳"}

    def test_duplicate_members_with_equal_values_coalesce_in_one_record(self) -> None:
        # The single overlap year publishes the measure under BOTH code blocks
        # with identical values. Both members land in one group; the column
        # coalesces them (equal value+unit) into the one measure rather than
        # failing as an ambiguous multi-member match.
        objs = (_classobj("cat02", [("01", "年齢"), ("33", "年齢")]),)
        rule = build_generic_rule(_TRADE_CLF, objs)
        assert rule is not None
        rows = (
            {"cat01": "A", "cat02": "01", "area": "00000", "time": "2020000000",
             "value": "50.6", "unit": "歳"},
            {"cat01": "A", "cat02": "33", "area": "00000", "time": "2020000000",
             "value": "50.6", "unit": "歳"},
        )
        out = apply_v2_rule(rows, _TRADE_CLF, rule, class_objs=objs)
        assert len(out) == 1
        assert out[0]["年齢"] == {"value": "50.6", "unit": "歳"}

    def test_duplicate_members_with_differing_values_raise_for_layer_d(self) -> None:
        # The coalesce is guarded: same-named members carrying *different*
        # values are not a vintage dual-coding but a genuine collision (no
        # single cell to surface). The fold raises a typed error, which the
        # auto path turns into a Layer D fallback — the same safe decline the
        # whole table took earlier, now scoped to the conflicting cell.
        objs = (_classobj("cat02", [("01", "年齢"), ("33", "年齢")]),)
        rule = build_generic_rule(_TRADE_CLF, objs)
        assert rule is not None
        rows = (
            {"cat01": "A", "cat02": "01", "area": "00000", "time": "2020000000",
             "value": "50.6", "unit": "歳"},
            {"cat01": "A", "cat02": "33", "area": "00000", "time": "2020000000",
             "value": "99.9", "unit": "歳"},
        )
        with pytest.raises(RoleResolutionError, match="matched"):
            apply_v2_rule(rows, _TRADE_CLF, rule, class_objs=objs)

    def test_value_role_coexisting_with_meta_axis_declines(self) -> None:
        # A meta-axis already spreads the measures across rows. A
        # single-member tab (VALUE) alongside it would emit a spurious `value`
        # 1:1 column that, after grouping, reads an arbitrary group member's
        # cell (rows[0]) — a non-deterministic, duplicate column. Decline to
        # Layer D rather than fold this ambiguous shape.
        clf = TableClassification((
            _axis("tab", AxisRole.VALUE),
            _axis("cat02", AxisRole.META_AXIS),
            _axis("time", AxisRole.TIME),
        ))
        assert build_generic_rule(clf, _TRADE_META) is None

    def test_hierarchical_meta_axis_declines(self) -> None:
        # Flatness gate: a meta-axis whose members carry a code hierarchy
        # (@level/@parentCode) folds a second dimension into its members —
        # trade's 合計/月次 × 数量/金額. Flat-pivoting it would spread that
        # hidden dimension into columns, so Layer A declines (→ Layer D); the
        # precise reshape is a rule's job, not the generic auto path.
        objs = (_classobj_hier("cat02", [
            ("120", "合計_数量", "1", None),
            ("150", "1月_数量", "2", "120"),
            ("160", "2月_数量", "2", "120"),
        ]),)
        assert build_generic_rule(_TRADE_CLF, objs) is None

    def test_flat_meta_axis_with_levels_still_pivots(self) -> None:
        # A meta-axis carrying @level but no real hierarchy (all one level, no
        # parent) is still flat — the clean 表章項目 case — so it pivots. The
        # gate keys on hierarchy (parent / depth), not on @level being present.
        objs = (_classobj_hier("cat02", [
            ("11", "金額", "1", None),
            ("12", "数量", "1", None),
        ]),)
        rule = build_generic_rule(_TRADE_CLF, objs)
        assert rule is not None
        assert [c.column for c in rule.output if c.source.where is not None] == ["金額", "数量"]
