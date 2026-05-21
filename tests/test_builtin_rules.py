"""Tests for the library-bundled rules.

DESIGN.md task #7's success criterion #1 is: "the three benchmark
tables each have a bundled rule that yields self-describing rows
when applied". The tests below pin that the three rules exist with
the expected shape; end-to-end validation against the live API
lives in the integration suite.
"""
from __future__ import annotations

from pyestat import load_builtin_rules


class TestBuiltinRulesPresence:
    def test_three_benchmark_tables_are_covered(self) -> None:
        # DESIGN.md commits to population estimates, quarterly GDP,
        # and foreign trade as the three benchmark tables. If any
        # of these YAMLs is renamed or removed, this test catches it
        # before a release.
        rules = load_builtin_rules()
        stat_codes = {r.match.statsCode for r in rules}
        assert {"00200524", "00100409", "00350300"} <= stat_codes


class TestBuiltinRuleShapes:
    """Each bundled rule's MVP-relevant decisions are pinned so a
    later edit cannot silently change the granularity tag the LLM
    will see."""

    def _by_stat(self, stat_code: str):
        for r in load_builtin_rules():
            if r.match.statsCode == stat_code:
                return r
        raise AssertionError(f"no builtin rule for {stat_code}")

    def test_population_uses_monthly_parser(self) -> None:
        rule = self._by_stat("00200524")
        assert rule.axes.time.id == "time"
        assert rule.axes.time.format == "monthly_e_stat"
        assert rule.axes.area is not None
        assert rule.value.type == "number"

    def test_gdp_uses_quarterly_parser_without_area(self) -> None:
        rule = self._by_stat("00100409")
        assert rule.axes.time.format == "quarterly_e_stat"
        # GDP has no area axis; encoding ``axes.area`` would make
        # FingerprintMatcher reject every actual GDP response.
        assert rule.axes.area is None

    def test_foreign_trade_uses_yearly_parser_with_area(self) -> None:
        rule = self._by_stat("00350300")
        assert rule.axes.time.format == "yearly"
        assert rule.axes.area is not None
        # value.type stays "number" at MVP — the conditional value
        # typing that this table really needs is on the Decision-D
        # expansion list, not in v1 of the schema.
        assert rule.value.type == "number"
