"""Tests for the library-bundled rules.

#30 retired the never-published v1 built-in rules. The auto path now
structures most benchmark tables generically (Layer A folds GDP and the
population estimates without a rule), so the bundle ships only the table
Layer A cannot fold on its own: foreign trade, whose ``cat02`` axis is a
hierarchical measure×period cross (#34 routes a hierarchical meta-axis to
Layer D; this built-in reshapes it with the #37/#39 modifiers).

These tests pin the loader contract, that the trade rule is matched at the
BUILTIN layer for its family, and that it folds the cross into self-describing
month rows — and that ``match.stats_code`` keeps it from claiming a
structurally identical table from another survey (#29).
"""
from __future__ import annotations

from pyestat import RuleV2, load_builtin_rules
from pyestat._endpoint import ClassObj
from pyestat._engine.apply import apply_v2_rule
from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)
from pyestat._engine.resolver import RuleLayer, resolve_v2


_TRADE_PATTERN = [AxisRole.CATEGORY, AxisRole.META_AXIS, AxisRole.AREA, AxisRole.TIME]
_TRADE_STATS_CODE = "00350300"


# A faithful slice of trade's cat02 (#37/#39): two grain-less unit members
# whose *value* is the unit string, three measure-family totals (level 1), and
# the level-2 month members hung under each family by @parentCode. One logical
# record (commodity 0101 × country 50103 × year 2026) is spread across it.
_TRADE_CAT02 = ClassObj(id="cat02", name="統計品目表の数量・金額", classes=(
    {"code": "100", "name": "単位1", "level": "1"},
    {"code": "110", "name": "単位2", "level": "1"},
    {"code": "120", "name": "合計_数量1", "level": "1"},
    {"code": "130", "name": "合計_数量2", "level": "1"},
    {"code": "140", "name": "合計_金額", "level": "1", "unit": "千円"},
    {"code": "150", "name": "1月_数量1", "level": "2", "parentCode": "120"},
    {"code": "160", "name": "1月_数量2", "level": "2", "parentCode": "130"},
    {"code": "170", "name": "1月_金額", "level": "2", "parentCode": "140", "unit": "千円"},
    {"code": "180", "name": "2月_数量1", "level": "2", "parentCode": "120"},
    {"code": "190", "name": "2月_数量2", "level": "2", "parentCode": "130"},
    {"code": "200", "name": "2月_金額", "level": "2", "parentCode": "140", "unit": "千円"},
))
_TRADE_CLASS_OBJS = (_TRADE_CAT02,)

# cat01 is the modest-confidence commodity axis; the rest are confident — the
# real table classifies exactly this way (verified against 0004049306).
_TRADE_CLASSIFICATION = TableClassification((
    AxisClassification("cat01", AxisRole.CATEGORY, Confidence.MEDIUM, ("test",)),
    AxisClassification("cat02", AxisRole.META_AXIS, Confidence.HIGH, ("test",)),
    AxisClassification("area", AxisRole.AREA, Confidence.HIGH, ("test",)),
    AxisClassification("time", AxisRole.TIME, Confidence.HIGH, ("test",)),
))

# The 単位 row's *value* is the unit string; the year totals (合計_*) ride along
# as a real table ships them. None of these grain-less level-1 members forms a
# month row, but 単位2's value reaches 数量2 as the broadcast unit.
#
# Modeled on the real table (0004049306): 単位1 is *defined in the metadata but
# ships no data row* — most commodities report only the 第2数量, so e-Stat omits
# the 単位1 observation. So 数量1 has a value but no unit member to read; the rule
# must still emit quantity1 with unit None (the #39 graceful path), not drop it.
_TRADE_ROWS = (
    {"cat01": "0101", "cat02": "110", "area": "50103", "time": "2026000000", "value": "ＮＯ"},
    {"cat01": "0101", "cat02": "120", "area": "50103", "time": "2026000000", "value": "11"},
    {"cat01": "0101", "cat02": "130", "area": "50103", "time": "2026000000", "value": "13"},
    {"cat01": "0101", "cat02": "140", "area": "50103", "time": "2026000000", "value": "76300", "unit": "千円"},
    {"cat01": "0101", "cat02": "150", "area": "50103", "time": "2026000000", "value": "5"},
    {"cat01": "0101", "cat02": "160", "area": "50103", "time": "2026000000", "value": "6"},
    {"cat01": "0101", "cat02": "170", "area": "50103", "time": "2026000000", "value": "35220", "unit": "千円"},
    {"cat01": "0101", "cat02": "180", "area": "50103", "time": "2026000000", "value": "8"},
    {"cat01": "0101", "cat02": "190", "area": "50103", "time": "2026000000", "value": "7"},
    {"cat01": "0101", "cat02": "200", "area": "50103", "time": "2026000000", "value": "41080", "unit": "千円"},
)


def _trade_rule() -> RuleV2:
    matches = [r for r in load_builtin_rules() if list(r.match.role_pattern) == _TRADE_PATTERN]
    assert len(matches) == 1, "expected exactly one bundled foreign-trade rule"
    return matches[0]


class TestBuiltinRuleContract:
    def test_all_bundled_rules_are_v2(self) -> None:
        # The auto path resolves by role pattern and considers only v2 rules; a
        # stray non-v2 rule in the bundle would silently never fire.
        assert all(isinstance(r, RuleV2) for r in load_builtin_rules())

    def test_bundle_covers_foreign_trade(self) -> None:
        # Flips the post-#30 "bundle is empty" pin: trade is the one benchmark
        # table Layer A declines (hierarchical meta-axis → Layer D), so it must
        # ship a built-in. Matched by the role pattern the live table classifies
        # to, and scoped to the trade statsCode (#29).
        rule = _trade_rule()
        assert rule.match.stats_code == _TRADE_STATS_CODE


class TestBuiltinBundleIsUnambiguous:
    """No two bundled rules may match the same table.

    A built-in collision is a packaging defect, not a caller error: the
    resolver degrades a same-layer built-in conflict to Layer D rather than
    raise (``docs/DESIGN.md`` Decision B — an internal cause falls back, only
    a caller-authored conflict surfaces). So a duplicate would silently lose
    the structure both rules meant to add, with no exception to flag it. This
    guard fails the build instead, so the conflict is caught before release.

    Two rules collide exactly when ``resolver._matches`` could pick both for
    one table: they share a role pattern *and* their statsCode scopes can be
    satisfied by a single table — neither pins a statsCode, or both pin the
    same one. A different pattern, or two different pinned families, can never
    match the same table, so they do not collide.
    """

    def test_no_two_bundled_rules_match_the_same_table(self) -> None:
        rules = list(load_builtin_rules())

        def collide(a: RuleV2, b: RuleV2) -> bool:
            if list(a.match.role_pattern) != list(b.match.role_pattern):
                return False
            a_code, b_code = a.match.stats_code, b.match.stats_code
            return a_code is None or b_code is None or a_code == b_code

        conflicts = [
            (list(a.match.role_pattern), a.match.stats_code, b.match.stats_code)
            for i, a in enumerate(rules)
            for b in rules[i + 1 :]
            if collide(a, b)
        ]
        assert not conflicts, (
            "bundled rules collide (same role pattern + overlapping statsCode); "
            f"each pair would both match one table: {conflicts}"
        )


class TestForeignTradeBuiltin:
    """The bundled trade rule folds cat02's measure×period cross into one row
    per month, each measure self-describing ({value, unit}): quantities carry
    the unit shipped as a 単位 member (#39), the amount keeps its own @unit.
    This is the table Layer A cannot fold, so the built-in is what makes trade
    structured on the auto path."""

    def test_resolver_picks_the_trade_rule_at_the_builtin_layer(self) -> None:
        resolved = resolve_v2(
            _TRADE_CLASSIFICATION,
            builtin=load_builtin_rules(),
            class_objs=_TRADE_CLASS_OBJS,
            stats_code=_TRADE_STATS_CODE,
        )
        assert resolved is not None
        assert resolved.layer is RuleLayer.BUILTIN

    def test_yields_a_flat_look_alike_to_layer_a_instead_of_emptying_it(self) -> None:
        # A structurally identical *flat*-meta table from another survey is the
        # silent-loss case: if the trade rule fired, its month/parent selectors
        # would match nothing and `key` would drop every member, folding the
        # table to zero rows with no exception to route it to Layer D. Scoped to
        # the trade family, the rule declines, so Layer A pivots the flat
        # meta-axis into a structured row instead — data kept, not emptied.
        flat_objs = (ClassObj(id="tab", name="表章項目", classes=(
            {"code": "01", "name": "指数", "level": "1"},
            {"code": "09", "name": "単位", "level": "1"},
        )),)
        flat = TableClassification((
            AxisClassification("cat01", AxisRole.CATEGORY, Confidence.MEDIUM, ("test",)),
            AxisClassification("tab", AxisRole.META_AXIS, Confidence.HIGH, ("test",)),
            AxisClassification("area", AxisRole.AREA, Confidence.HIGH, ("test",)),
            AxisClassification("time", AxisRole.TIME, Confidence.HIGH, ("test",)),
        ))
        resolved = resolve_v2(
            flat, builtin=load_builtin_rules(), class_objs=flat_objs, stats_code="00200521",
        )
        assert resolved is not None
        assert resolved.layer is RuleLayer.GENERIC

    def test_folds_cross_into_month_rows_with_self_describing_measures(self) -> None:
        resolved = resolve_v2(
            _TRADE_CLASSIFICATION,
            builtin=load_builtin_rules(),
            class_objs=_TRADE_CLASS_OBJS,
            stats_code=_TRADE_STATS_CODE,
        )
        out = apply_v2_rule(
            _TRADE_ROWS, _TRADE_CLASSIFICATION, resolved.rule, class_objs=_TRADE_CLASS_OBJS,
        )
        by_month = {r["month"]: r for r in out}
        assert set(by_month) == {"1月", "2月"}

        jan = by_month["1月"]
        assert jan["commodity"] == {"code": "0101", "label": "0101"}
        assert jan["country"] == {"code": "50103", "label": "50103"}
        assert jan["year"]["normalized"] == "2026"
        assert jan["year"]["granularity"] == "yearly"
        # 数量2 carries the unit broadcast from its 単位2 member (#39); the amount
        # keeps its own @unit. 数量1's unit member (単位1) ships no data row on the
        # real table, so quantity1 keeps its value but reports unit None — the
        # graceful path, not a dropped column. Year totals (合計_*) do not leak
        # into the month rows.
        assert jan["quantity1"] == {"value": "5", "unit": None}
        assert jan["quantity2"] == {"value": "6", "unit": "ＮＯ"}
        assert jan["amount"] == {"value": "35220", "unit": "千円"}

    def test_quantity1_unit_resolves_when_the_unit1_member_is_shipped(self) -> None:
        # The complement of the fold test above: ~4% of trade tables actually use
        # the 第1数量 and then DO ship a 単位1 member whose value is the unit (always
        # ＮＯ in the corpus). When present, quantity1 must carry that unit — this
        # exercises quantity1's own unit_from wiring, not just quantity2's.
        # Verified against the real 海上コンテナ貨物 table 0003228227.
        rows = (
            {"cat01": "0101", "cat02": "100", "area": "50103", "time": "2026000000", "value": "ＮＯ"},
        ) + _TRADE_ROWS
        resolved = resolve_v2(
            _TRADE_CLASSIFICATION,
            builtin=load_builtin_rules(),
            class_objs=_TRADE_CLASS_OBJS,
            stats_code=_TRADE_STATS_CODE,
        )
        out = apply_v2_rule(
            rows, _TRADE_CLASSIFICATION, resolved.rule, class_objs=_TRADE_CLASS_OBJS,
        )
        assert {r["quantity1"]["unit"] for r in out} == {"ＮＯ"}


# statsCode 00350300's second structural group — 税関別 品別国別表 — is the same
# measure×period cross plus a 税関 (cat03) axis, so it classifies as
# [category, meta-axis, category, area, time] (verified against 0003258368). Two
# category axes (品目 cat01 / 税関 cat03) need axis-id addressing (#38) to map each
# to its own column; the fold is otherwise identical to the 品別国別表 group.
_CUSTOMS_PATTERN = [
    AxisRole.CATEGORY, AxisRole.META_AXIS, AxisRole.CATEGORY, AxisRole.AREA, AxisRole.TIME,
]
_CUSTOMS_CAT03 = ClassObj(id="cat03", name="税関", classes=(
    {"code": "50103", "name": "羽田", "level": "1"},
    {"code": "50104", "name": "成田", "level": "1"},
))
_CUSTOMS_CLASS_OBJS = (_TRADE_CAT02, _CUSTOMS_CAT03)
_CUSTOMS_CLASSIFICATION = TableClassification((
    AxisClassification("cat01", AxisRole.CATEGORY, Confidence.MEDIUM, ("test",)),
    AxisClassification("cat02", AxisRole.META_AXIS, Confidence.HIGH, ("test",)),
    AxisClassification("cat03", AxisRole.CATEGORY, Confidence.MEDIUM, ("test",)),
    AxisClassification("area", AxisRole.AREA, Confidence.HIGH, ("test",)),
    AxisClassification("time", AxisRole.TIME, Confidence.HIGH, ("test",)),
))
# The group-1 cross under two customs offices: the pivot groups by every non-meta
# axis (incl. cat03), so each (commodity × customs × country × month) is one row.
_CUSTOMS_ROWS = tuple(
    {**row, "cat03": customs}
    for customs in ("50103", "50104")
    for row in _TRADE_ROWS
)


def _customs_rule() -> RuleV2:
    matches = [r for r in load_builtin_rules() if list(r.match.role_pattern) == _CUSTOMS_PATTERN]
    assert len(matches) == 1, "expected exactly one bundled 税関別 trade rule"
    return matches[0]


class TestForeignTradeCustomsBuiltin:
    """The 税関別 sibling rule folds the same cross while carrying the 税関 axis as
    its own column (#38). Covering both structural groups makes the whole trade
    family (00350300) structured on the auto path — and exercises the built-in
    mechanism on a real two-category-axis table."""

    def test_bundle_covers_the_customs_group(self) -> None:
        assert _customs_rule().match.stats_code == _TRADE_STATS_CODE

    def test_resolver_picks_the_customs_rule_at_the_builtin_layer(self) -> None:
        resolved = resolve_v2(
            _CUSTOMS_CLASSIFICATION,
            builtin=load_builtin_rules(),
            class_objs=_CUSTOMS_CLASS_OBJS,
            stats_code=_TRADE_STATS_CODE,
        )
        assert resolved is not None
        assert resolved.layer is RuleLayer.BUILTIN

    def test_folds_cross_and_carries_the_customs_axis_as_its_own_column(self) -> None:
        resolved = resolve_v2(
            _CUSTOMS_CLASSIFICATION,
            builtin=load_builtin_rules(),
            class_objs=_CUSTOMS_CLASS_OBJS,
            stats_code=_TRADE_STATS_CODE,
        )
        out = apply_v2_rule(
            _CUSTOMS_ROWS, _CUSTOMS_CLASSIFICATION, resolved.rule, class_objs=_CUSTOMS_CLASS_OBJS,
        )
        # One row per (customs × month); cat03 is folded into its own labeled column,
        # not collapsed with the commodity axis.
        assert {(r["customs"]["label"], r["month"]) for r in out} == {
            ("羽田", "1月"), ("羽田", "2月"), ("成田", "1月"), ("成田", "2月"),
        }
        haneda_jan = next(r for r in out if r["customs"]["label"] == "羽田" and r["month"] == "1月")
        assert haneda_jan["commodity"] == {"code": "0101", "label": "0101"}
        assert haneda_jan["quantity2"] == {"value": "6", "unit": "ＮＯ"}
        assert haneda_jan["amount"] == {"value": "35220", "unit": "千円"}
