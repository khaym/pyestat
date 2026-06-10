"""Tests for the axis classifier (Layer A keystone, task #21).

The classifier labels each axis of an e-Stat table with a *role*
(time / area / value / category / meta-axis / unknown) and a discrete
*confidence tier* (high / medium / low), deterministically, from table
metadata alone — no LLM, no network (PROPOSAL-AXIS-ROLE-INFERENCE Open
question 1).

These tests encode the business rules of role inference. The headline
rule is the **mis-pivot guard**: an axis that genuinely splits one
logical record across rows (trade's ``cat02`` = 数量 / 金額 / 単位) must be
recognised as ``meta-axis`` so it can be pivoted, while an axis that
merely *looks* unit-bearing (population's ``cat02`` = 男女別, which carries
heterogeneous ``@unit``) must stay ``category`` — mis-pivoting it would
silently corrupt data.

Fixtures are hand-derived from the 2026-05 survey of six statsCodes and
kept self-contained here (the raw ``work/research`` dumps are gitignored
and must not be a test dependency).
"""
from __future__ import annotations

from typing import Any

from pyestat._endpoint import ClassObj
from pyestat._engine.classifier import (
    AxisRole,
    Confidence,
    TableClassification,
    classify,
    is_flat_axis,
)


# --- fixtures --------------------------------------------------------------


def _axis(axis_id: str, name: str, *members: Any) -> ClassObj:
    """Build a ClassObj from (code, name) pairs, bare names, or dicts.

    Bare strings become a member whose code equals its name; tuples are
    ``(code, name)``; dicts are passed through so a test can attach
    ``@unit`` / ``level`` when the rule under test cares.
    """
    classes: list[dict[str, Any]] = []
    for m in members:
        if isinstance(m, dict):
            classes.append(m)
        elif isinstance(m, tuple):
            code, nm = m
            classes.append({"code": code, "name": nm, "level": "1"})
        else:
            classes.append({"code": m, "name": m, "level": "1"})
    return ClassObj(id=axis_id, name=name, classes=tuple(classes))


# Representative single axes pulled from the survey.
_TIME_AXIS = _axis(
    "time", "時間軸（年・月）", ("2011001212", "2011年12月"), ("2011001111", "2011年11月")
)
_AREA_AXIS = _axis("area", "地域", ("00000", "全国"), ("13100", "東京都区部"))
_TAB_SINGLE = _axis("tab", "表章項目", ("1", "金額"))  # GDP / household
_TAB_MULTI = _axis(  # CPI: 7 value types
    "tab", "表章項目", ("1", "指数"), ("2", "前月比"), ("3", "前年同月比")
)
# trade cat02 — the genuine meta-axis (measure spread)
_TRADE_META = _axis(
    "cat02", "統計品目表の数量・金額",
    ("100", "単位1"), ("120", "合計_数量1"), ("140", "合計_金額"),
)
# population cat02 — a sex *category* that nonetheless carries @unit;
# the mis-pivot trap.
_POP_SEX = _axis(
    "cat02", "男女別人口性比",
    {"code": "0", "name": "総人口・男女計", "unit": "千人"},
    {"code": "1", "name": "総人口・男", "unit": "千人"},
    {"code": "2", "name": "総人口・女", "unit": "千人"},
    {"code": "3", "name": "総人口・人口性比", "unit": "女＝１００"},
)


def _roles(tc: TableClassification) -> dict[str, AxisRole]:
    return {a.axis_id: a.role for a in tc.axes}


def _conf(tc: TableClassification, axis_id: str) -> Confidence:
    return next(a.confidence for a in tc.axes if a.axis_id == axis_id)


def _role(tc: TableClassification, axis_id: str) -> AxisRole:
    return next(a.role for a in tc.axes if a.axis_id == axis_id)


# --- time ------------------------------------------------------------------


class TestTimeRole:
    def test_conventional_id_and_date_codes_are_high(self) -> None:
        # The two defining signals concur: axis_id == "time" and the
        # member codes have e-Stat's 10-digit date shape.
        tc = classify([_TIME_AXIS])
        assert _role(tc, "time") == AxisRole.TIME
        assert _conf(tc, "time") == Confidence.HIGH

    def test_fiscal_year_codes_still_count_as_dates(self) -> None:
        # GDP fiscal-year codes are 10-digit but not parseable by the
        # strict monthly/quarterly/yearly parsers (code[4:6] == "10").
        # The role signal is the date *shape*, not a successful parse.
        gdp_time = _axis("time", "時間軸（年度）", ("1995100000", "1995年度"))
        assert _role(classify([gdp_time]), "time") == AxisRole.TIME

    def test_time_id_without_date_codes_is_medium(self) -> None:
        # Only one signal present (the conventional id); neither the name
        # (no 時間軸) nor the codes (not date-shaped) corroborate, so
        # confidence drops a tier.
        weird = _axis("time", "区分", ("A", "甲"), ("B", "乙"))
        tc = classify([weird])
        assert _role(tc, "time") == AxisRole.TIME
        assert _conf(tc, "time") == Confidence.MEDIUM


# --- area ------------------------------------------------------------------


class TestAreaRole:
    def test_domestic_jis_area_is_high(self) -> None:
        tc = classify([_AREA_AXIS])
        assert _role(tc, "area") == AxisRole.AREA
        assert _conf(tc, "area") == Confidence.HIGH

    def test_foreign_country_axis_is_still_area(self) -> None:
        # Trade's area axis carries country codes, not JIS — the role is
        # still `area`; the code *vocabulary* is task #4's concern.
        country = _axis("area", "国", ("50103", "103_大韓民国"), ("50106", "106_台湾"))
        assert _role(classify([country]), "area") == AxisRole.AREA

    def test_area_id_without_corroboration_is_medium(self) -> None:
        bare = _axis("area", "区分", ("X", "甲"))
        assert _conf(classify([bare]), "area") == Confidence.MEDIUM


# --- value vs meta-axis on the 表章項目 axis --------------------------------


class TestTabAxis:
    def test_single_value_type_is_value(self) -> None:
        # One value type → the cell `$` is directly the value; no pivot.
        tc = classify([_TAB_SINGLE])
        assert _role(tc, "tab") == AxisRole.VALUE
        assert _conf(tc, "tab") == Confidence.HIGH

    def test_multiple_value_types_is_meta_axis(self) -> None:
        # >1 value type → one logical record is split across N rows.
        tc = classify([_TAB_MULTI])
        assert _role(tc, "tab") == AxisRole.META_AXIS
        assert _conf(tc, "tab") == Confidence.HIGH


# --- meta-axis on a non-tab axis: the mis-pivot guard ----------------------


class TestMisPivotGuard:
    def test_trade_cat02_is_meta_axis(self) -> None:
        # Axis name (数量・金額) and member names (単位 / 数量 / 金額) carry the
        # measure-spread lexicon → a genuine meta-axis, pivotable by #22.
        tc = classify([_TRADE_META])
        assert _role(tc, "cat02") == AxisRole.META_AXIS
        assert tc.clears()  # ≥ medium, so it does not fall to Layer D

    def test_population_sex_axis_is_category_not_meta(self) -> None:
        # The headline guard: heterogeneous @unit (千人 / 女＝１００) must NOT
        # trigger meta-axis. 男女別 is a category; pivoting it corrupts data.
        tc = classify([_POP_SEX])
        assert _role(tc, "cat02") == AxisRole.CATEGORY
        assert _role(tc, "cat02") != AxisRole.META_AXIS

    def test_lone_unit_member_is_low_confidence_unknown(self) -> None:
        # A single stray 単位 member with no supporting axis-name signal is
        # an *ambiguous* meta candidate — better routed to Layer D (low)
        # than mis-pivoted. This is the "typical meta-axis miss" of Q5.
        ambiguous = _axis("cat02", "区分", "甲", "乙", "単位", "丙")
        tc = classify([ambiguous])
        assert _role(tc, "cat02") == AxisRole.UNKNOWN
        assert _conf(tc, "cat02") == Confidence.LOW
        assert not tc.clears()  # routes to Layer D under the default gate


# --- category by elimination & table-level aggregation ---------------------


class TestCategoryAndAggregation:
    def test_plain_dimension_axis_is_category_medium(self) -> None:
        cat = _axis("cat01", "産業・企業規模", "産業計", "製造業", "建設業")
        tc = classify([cat])
        assert _role(tc, "cat01") == AxisRole.CATEGORY
        assert _conf(tc, "cat01") == Confidence.MEDIUM

    def test_role_pattern_preserves_axis_order(self) -> None:
        tc = classify([_TAB_MULTI, _AREA_AXIS, _TIME_AXIS])
        assert tc.role_pattern == (AxisRole.META_AXIS, AxisRole.AREA, AxisRole.TIME)

    def test_clears_is_weakest_link(self) -> None:
        # One low-confidence axis drags the whole table below the gate.
        ambiguous = _axis("cat02", "区分", "甲", "単位")
        tc = classify([_TIME_AXIS, ambiguous])
        assert _conf(tc, "time") == Confidence.HIGH
        assert _conf(tc, "cat02") == Confidence.LOW
        assert not tc.clears(Confidence.MEDIUM)

    def test_classification_is_deterministic(self) -> None:
        # No randomness / LLM on the data path: identical input → identical
        # output, every time (Open question 1).
        axes = [_TAB_MULTI, _TRADE_META, _AREA_AXIS, _TIME_AXIS]
        assert classify(axes) == classify(axes)


# --- data-driven meta-axis detection (rows= path, vocabulary-free) ---------


class TestDataDrivenMeta:
    """When the fetched data rows are supplied, a non-tab meta-axis is found
    structurally — a unit-string member coexisting with numeric members —
    with no Japanese keyword lexicon (PROPOSAL Open question 7)."""

    def test_unit_string_member_makes_meta_without_vocabulary(self) -> None:
        # Generic axis name (no 数量/金額/単位): the ONLY signal is that one
        # member's cells are unit strings while others are numeric.
        axis = _axis("cat02", "区分", "100", "120", "140")
        rows = [
            {"cat02": "100", "value": "ＮＯ"}, {"cat02": "100", "value": "KG"},
            {"cat02": "120", "value": "12345"}, {"cat02": "120", "value": "678"},
            {"cat02": "140", "value": "98765"},
        ]
        tc = classify([axis], rows=rows)
        assert _role(tc, "cat02") == AxisRole.META_AXIS
        assert _conf(tc, "cat02") == Confidence.HIGH

    def test_all_numeric_axis_with_rows_is_category(self) -> None:
        # population 男女別: counts plus a numeric 性比 ratio — no unit-string
        # member, so it stays a category even though magnitudes differ.
        axis = _axis("cat02", "男女別人口性比", "0", "1", "2", "3")
        rows = [
            {"cat02": "0", "value": "10000"}, {"cat02": "1", "value": "5100"},
            {"cat02": "2", "value": "4900"}, {"cat02": "3", "value": "96.1"},
        ]
        tc = classify([axis], rows=rows)
        assert _role(tc, "cat02") == AxisRole.CATEGORY

    def test_suppression_markers_do_not_fake_meta(self) -> None:
        # Mostly "-" (suppressed) cells over an otherwise numeric axis must
        # not be read as unit strings — the household confounder.
        axis = _axis("cat01", "区分", "A", "B")
        rows = [
            {"cat01": "A", "value": "-"}, {"cat01": "A", "value": "100"},
            {"cat01": "B", "value": "-"}, {"cat01": "B", "value": "200"},
        ]
        tc = classify([axis], rows=rows)
        assert _role(tc, "cat01") != AxisRole.META_AXIS

    def test_lexicon_is_only_a_medium_fallback_without_rows(self) -> None:
        # Without data, the retired lexicon still gives a best-effort meta
        # guess, but capped at medium (it is no longer load-bearing).
        tc = classify([_TRADE_META])
        assert _role(tc, "cat02") == AxisRole.META_AXIS
        assert _conf(tc, "cat02") == Confidence.MEDIUM


# --- representative whole-table classifications (6 surveyed statsCodes) -----


class TestRepresentativeTables:
    """Pin the role pattern of one representative table per surveyed
    statsCode. These are the "確認できる" checks of #21's success
    condition; systematic gold-set scoring is #24."""

    def test_cpi_tab_is_meta(self) -> None:
        cpi = [
            _TAB_MULTI,
            _axis("cat01", "平成17年基準品目", "総合", "食料"),
            _AREA_AXIS,
            _TIME_AXIS,
        ]
        tc = classify(cpi)
        assert tc.role_pattern == (
            AxisRole.META_AXIS, AxisRole.CATEGORY, AxisRole.AREA, AxisRole.TIME,
        )
        assert tc.clears()

    def test_household_tab_single_is_value(self) -> None:
        household = [
            _TAB_SINGLE,
            _axis("cat01", "用途分類", "世帯数分布", "集計世帯数"),
            _axis("cat02", "世帯区分", "二人以上の世帯"),
            _axis("area", "地域区分", ("00000", "全国")),
            _axis("time", "時間軸（月次）", ("1985000101", "1985年1月")),
        ]
        tc = classify(household)
        assert tc.role_pattern == (
            AxisRole.VALUE, AxisRole.CATEGORY, AxisRole.CATEGORY,
            AxisRole.AREA, AxisRole.TIME,
        )

    def test_wage_tab_multi_is_meta(self) -> None:
        wage = [
            _axis("tab", "表章項目", "年齢", "勤続年数", "所定内実労働時間数"),
            _axis("cat01", "産業・企業規模", "産業計", "製造業"),
            _axis("cat02", "在留資格区分", "外国人労働者"),
            _axis("cat03", "民・公区分", "民営事業所"),
            _axis("time", "時間軸（2020～2023）", ("2023000000", "2023年")),
        ]
        tc = classify(wage)
        assert tc.role_pattern == (
            AxisRole.META_AXIS, AxisRole.CATEGORY, AxisRole.CATEGORY,
            AxisRole.CATEGORY, AxisRole.TIME,
        )

    def test_population_has_no_meta_axis(self) -> None:
        population = [
            _axis("cat01", "年齢各歳", "0歳", "1歳"),
            _POP_SEX,
            _axis("area", "全国150001", ("00000", "全国")),
            _axis("time", "時間軸(年次)", ("1991000000", "1991年")),
        ]
        tc = classify(population)
        assert tc.role_pattern == (
            AxisRole.CATEGORY, AxisRole.CATEGORY, AxisRole.AREA, AxisRole.TIME,
        )
        assert AxisRole.META_AXIS not in tc.role_pattern

    def test_gdp_tab_single_is_value(self) -> None:
        gdp = [
            _TAB_SINGLE,
            _axis("cat01", "供給と需要", "国内FISIM産出額"),
            _axis("time", "時間軸（年度）", ("1995100000", "1995年度")),
        ]
        tc = classify(gdp)
        assert tc.role_pattern == (
            AxisRole.VALUE, AxisRole.CATEGORY, AxisRole.TIME,
        )

    def test_trade_cat02_is_meta_and_table_clears(self) -> None:
        trade = [
            _axis("cat01", "統計品目表(輸出)", ("010110000", "010110000")),
            _TRADE_META,
            _axis("area", "国", ("50103", "103_大韓民国")),
            _axis("time", "時間軸(年次)", ("2005000000", "2005年")),
        ]
        tc = classify(trade)
        assert tc.role_pattern == (
            AxisRole.CATEGORY, AxisRole.META_AXIS, AxisRole.AREA, AxisRole.TIME,
        )
        assert tc.clears()  # trade is pivotable, not routed to Layer D


class TestIsFlatAxis:
    """``is_flat_axis`` reads e-Stat's @level / @parentCode to tell a clean,
    flat measure axis (the 表章項目 convention) from a *cross* axis that folds
    a second dimension into its members (trade's 合計/月次 × 数量/金額). The #34
    auto-pivot fires only on a flat meta-axis; a hierarchical one rides Layer D.

    Business rule confirmed by the 2026-06 survey (8 statsCodes, 186 axes):
    every clean measure axis is flat; the only hierarchical meta-axis is
    trade's cat02 cross.
    """

    def test_flat_when_one_level_and_no_parent(self) -> None:
        # 表章項目: members at a single level, none naming a parent.
        assert is_flat_axis(_TAB_MULTI) is True

    def test_flat_when_level_is_empty(self) -> None:
        # Real GDP tab members carry @level="" — absence of depth, not a tier.
        tab = _axis("tab", "表章項目",
                    {"code": "11", "name": "金額", "level": ""},
                    {"code": "12", "name": "前年同期比", "level": ""})
        assert is_flat_axis(tab) is True

    def test_hierarchical_when_a_member_has_a_parent(self) -> None:
        # trade cat02 in miniature: 合計 (root) over monthly children.
        cross = _axis("cat02", "統計品目表の数量・金額",
                      {"code": "120", "name": "合計_数量", "level": "1"},
                      {"code": "150", "name": "1月_数量", "level": "2", "parentCode": "120"})
        assert is_flat_axis(cross) is False

    def test_hierarchical_when_a_deeper_level_appears(self) -> None:
        # A level beyond {"", "1"} is a hierarchy even without an explicit
        # parentCode in the sampled rows.
        deep = _axis("cat01", "用途分類",
                     {"code": "1", "name": "合計", "level": "1"},
                     {"code": "2", "name": "食料", "level": "3"})
        assert is_flat_axis(deep) is False
