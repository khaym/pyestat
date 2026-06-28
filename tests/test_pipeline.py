"""Tests for the post-fetch transformation pipeline.

``run_pipeline`` owns the order — classify → aggregate-select → resolve →
apply — and the Layer A–D routing plus the surface-vs-degrade error policy
(ARCHITECTURE.md). These tests encode that routing rule directly
on hand-built rows + class metadata, with **no HTTP transport to mock** — the
behavior a caller observes through ``EstatClient.get_stats_data`` minus the
fetch. Extracting the pipeline out of the endpoint is what makes this possible.
"""
from __future__ import annotations

from typing import Any

import pytest

from pyestat._endpoint import ClassObj
from pyestat._engine.pipeline import run_pipeline
from pyestat._engine.rule import RuleV2
from pyestat.errors import RuleAuthoringError


def _classobj(axis_id: str, name: str, members: list[dict[str, Any]]) -> ClassObj:
    return ClassObj(id=axis_id, name=name, classes=tuple(members))


# A time + area + tab(single member) table classifies deterministically as
# time / area / value, so it resolves through Layer A (a generic 1:1 rule).
_CLASS_OBJS = (
    _classobj("time", "時間軸（年次）", [
        {"code": "2020000000", "name": "2020年"},
        {"code": "2021000000", "name": "2021年"},
    ]),
    _classobj("area", "全国", [
        {"code": "13000", "name": "東京都"},
        {"code": "27000", "name": "大阪府"},
    ]),
    _classobj("tab", "表章項目", [{"code": "020", "name": "人口"}]),
)
_ROWS = (
    {"time": "2020000000", "area": "13000", "tab": "020", "value": "100", "unit": "人"},
    {"time": "2021000000", "area": "27000", "tab": "020", "value": "200", "unit": "人"},
)

# A rule that matches the table's role pattern but cannot apply: it declares a
# *strict* monthly format for a yearly code, so the time reader raises a typed
# TimeFormatError (a RuleAuthoringError) on the first row.
_FAILING_RULE = RuleV2.model_validate({
    "schema_version": "2",
    "match": {"role_pattern": ["time", "area", "value"]},
    "output": [
        {"column": "period", "source": {"role": "time"}, "transform": "monthly_e_stat"},
        {"column": "area", "source": {"role": "area"}},
        {"column": "value", "source": {"role": "value"}},
    ],
})


def _run(rule: Any, *, aggregates: str = "include", user_rules=(), builtin_rules=()) -> tuple:
    return run_pipeline(
        _ROWS,
        _CLASS_OBJS,
        {},
        "0000",
        rule,
        aggregates,
        user_rules=list(user_rules),
        project_rules=[],
        builtin_rules=list(builtin_rules),
    )


def _has_measure(row: dict[str, Any]) -> bool:
    return any(isinstance(v, dict) and {"value", "unit"} <= v.keys() for v in row.values())


def _has_time_cell(row: dict[str, Any]) -> bool:
    # A time cell carries the granularity tag, not just a normalized string;
    # require both so a dropped granularity is caught.
    return any(
        isinstance(v, dict) and {"normalized", "granularity"} <= v.keys()
        for v in row.values()
    )


class TestModeRouting:
    def test_raw_mode_returns_layer2_rows_verbatim(self) -> None:
        # ``None`` never structures: flat scalars pass straight through.
        assert _run(None) == _ROWS

    def test_heuristic_mode_keeps_every_axis_as_a_cell_via_layer_d(self) -> None:
        out = _run("heuristic")
        assert all(_has_measure(r) and _has_time_cell(r) for r in out)
        # Layer D keeps every axis — including the single-member `tab` — as a
        # cell; the presence of `tab` is what tells Layer D from Layer A below.
        assert all("tab" in r for r in out)

    def test_auto_folds_the_value_axis_via_a_generic_layer_a_rule(self) -> None:
        out = _run("auto")
        assert all(_has_measure(r) and _has_time_cell(r) for r in out)
        # The generic 1:1 rule folds the single-member `tab` axis into the
        # measure, so `tab` is absent — the shape that distinguishes Layer A.
        assert all(set(r) == {"time", "area", "value"} for r in out)
        # The yearly code's granularity survives the structuring.
        time_cells = [v for r in out for v in r.values() if "granularity" in v]
        assert all(c["granularity"] == "yearly" for c in time_cells)

    def test_an_explicit_rule_is_applied_directly(self) -> None:
        rule = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["time", "area", "value"]},
            "output": [{"column": "value", "source": {"role": "value"}}],
        })
        out = _run(rule)
        assert [set(r) for r in out] == [{"value"}, {"value"}]


class TestProvenanceRouting:
    """A caller-authored rule that fails surfaces; a library-provided one
    degrades to Layer D (ARCHITECTURE.md) — the same input, routed by provenance."""

    def test_auto_surfaces_a_caller_authored_rule_failure(self) -> None:
        with pytest.raises(RuleAuthoringError):
            _run("auto", user_rules=[_FAILING_RULE])

    def test_auto_degrades_a_built_in_rule_failure_to_layer_d(self) -> None:
        out = _run("auto", builtin_rules=[_FAILING_RULE])  # does not raise
        # Degraded to Layer D — every axis kept as a cell (`tab` present), not
        # silently replaced by the generic Layer A rule (which folds `tab`).
        assert all("tab" in r and _has_measure(r) and _has_time_cell(r) for r in out)


class TestStatsCodeWiring:
    """``run_pipeline`` lifts the table's statsCode out of ``TABLE_INF`` and
    hands it to resolution, so a family-scoped built-in can fire only on
    its own survey. Without this wiring a ``match.stats_code`` rule could never
    match on the auto path."""

    _SCOPED_RULE = RuleV2.model_validate({
        "schema_version": "2",
        "match": {"role_pattern": ["time", "area", "value"], "stats_code": "00350300"},
        # A 1:1 rule that drops every axis but the value, so its output shape
        # ({"only_value"}) is unmistakably distinct from the Layer A generic
        # ({"time", "area", "value"}) — the test reads which rule won.
        "output": [{"column": "only_value", "source": {"role": "value"}}],
    })

    def _run_with_stat_name(self, stat_code: str | None):
        table_inf = {"STAT_NAME": {"@code": stat_code}} if stat_code is not None else {}
        return run_pipeline(
            _ROWS, _CLASS_OBJS, table_inf, "0000", "auto", "include",
            user_rules=[], project_rules=[], builtin_rules=[self._SCOPED_RULE],
        )

    def test_scoped_builtin_fires_when_table_statscode_matches(self) -> None:
        out = self._run_with_stat_name("00350300")
        assert [set(r) for r in out] == [{"only_value"}, {"only_value"}]

    def test_scoped_builtin_skips_other_family_and_falls_to_layer_a(self) -> None:
        out = self._run_with_stat_name("00200521")
        assert all(set(r) == {"time", "area", "value"} for r in out)

    def test_missing_stat_name_falls_to_layer_a(self) -> None:
        out = self._run_with_stat_name(None)
        assert all(set(r) == {"time", "area", "value"} for r in out)


class TestAggregateSelection:
    """The aggregate filter runs before any rule, so every mode honors it."""

    _HIER_CLASS_OBJS = (
        _classobj("cat01", "用途分類", [
            {"code": "0", "name": "総数", "level": "1"},
            {"code": "1", "name": "食料", "level": "2", "parentCode": "0"},
        ]),
        _classobj("time", "時間軸（年次）", [{"code": "2020000000", "name": "2020年"}]),
    )
    _HIER_ROWS = (
        {"cat01": "0", "time": "2020000000", "value": "13"},  # 総数 (aggregate)
        {"cat01": "1", "time": "2020000000", "value": "3"},   # 食料 (leaf, here)
    )

    def test_exclude_drops_the_aggregate_before_raw_passthrough(self) -> None:
        out = run_pipeline(
            self._HIER_ROWS,
            self._HIER_CLASS_OBJS,
            {},
            "0000",
            None,  # raw: prove the filter ran before the (no-op) rule
            "exclude",
            user_rules=[],
            project_rules=[],
            builtin_rules=[],
        )
        assert out == ({"cat01": "1", "time": "2020000000", "value": "3"},)
