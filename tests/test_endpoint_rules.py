"""Tests for the rule= integration into EstatClient.get_stats_data.

The rule engine (Layer 3) plugs into the endpoint client (Layer 2). These
tests prove the request-path wiring (#28), not the resolver / apply
mechanics (those have isolated coverage in test_resolver / test_apply_v2 /
test_layer_d). Behavior modes:

* ``rule=None`` — raw mode (axis_id-keyed dicts, no transformation).
* ``rule="auto"`` (default) — classify the axes, then resolve through
  Layers C > B > A > D: a matching v2 rule (user/project, then built-in),
  else a Layer A generic rule for a clean table, else Layer D.
* ``rule="heuristic"`` — Layer D fallback (#23): best-effort ``time``
  normalization plus additive labels, preserving raw data; bypasses rules.
* ``rule=RuleV2(...)`` — a v2 rule applied directly (the escape hatch),
  bypassing resolution.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pyestat._endpoint import EstatClient
from pyestat._http import EstatHttpClient
from pyestat._engine.rule import RuleV2
from pyestat.errors import RoleResolutionError, TimeFormatError, UnknownTransformError


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _make_client(
    payload: dict[str, Any], *, builtin_rules=None, user_rules=None
) -> EstatClient:
    queue: Iterator[dict[str, Any]] = iter([payload])
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=next(queue)))
    http = EstatHttpClient(app_id="x", transport=transport, sleep=lambda _s: None)
    return EstatClient(http=http, builtin_rules=builtin_rules, user_rules=user_rules)


def _population_payload() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "get_stats_data_population_sample.json").read_text(encoding="utf-8"))


def _payload_with_area(base: dict[str, Any]) -> dict[str, Any]:
    """Inject an ``area`` axis into a copy of the given payload.

    The fixture is simplified compared to the live 0003443838 table —
    real population data has an ``area`` axis that the bundled rule
    relies on. Tests that exercise the bundled rule patch the fixture
    here rather than maintain a second on-disk copy.
    """
    out = copy.deepcopy(base)
    class_inf = out["GET_STATS_DATA"]["STATISTICAL_DATA"]["CLASS_INF"]
    class_inf["CLASS_OBJ"].append(
        {"@id": "area", "@name": "全国", "CLASS": {"@code": "00000", "@name": "全国", "@level": "1"}}
    )
    for value in out["GET_STATS_DATA"]["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]:
        value["@area"] = "00000"
        # Replace the time code with a real monthly e-Stat code so the
        # monthly parser the builtin rule names actually consumes it.
        value["@time"] = "2022000101"
    for cls in class_inf["CLASS_OBJ"]:
        if cls["@id"] == "time":
            cls["@name"] = "時間軸（年月日現在）"
            cls["CLASS"] = {"@code": "2022000101", "@name": "2022年1月", "@level": "4"}
    return out


def _meta_axis_payload() -> dict[str, Any]:
    """A table whose ``tab`` axis carries several value types — a single
    meta-axis (role pattern ``[meta-axis, time]``).

    With a matching builtin pivot rule it folds via Layer B; with none, Layer A
    now auto-generates a pivot rule (#34) and folds it generically. Built
    inline (not an on-disk fixture) because its whole point is a shape the
    bundled fixtures deliberately avoid.
    """
    return {
        "GET_STATS_DATA": {
            "RESULT": {"STATUS": 0},
            "STATISTICAL_DATA": {
                "RESULT_INF": {"TOTAL_NUMBER": 2},
                "TABLE_INF": {"@id": "T"},
                "CLASS_INF": {"CLASS_OBJ": [
                    {"@id": "tab", "@name": "表章項目", "CLASS": [
                        {"@code": "001", "@name": "数量"},
                        {"@code": "002", "@name": "金額"},
                    ]},
                    {"@id": "time", "@name": "時間軸（年次）",
                     "CLASS": {"@code": "2020000000", "@name": "2020年"}},
                ]},
                "DATA_INF": {"VALUE": [
                    {"@tab": "001", "@time": "2020000000", "$": "5"},
                    {"@tab": "002", "@time": "2020000000", "$": "1000"},
                ]},
            },
        }
    }


def _hierarchical_meta_axis_payload() -> dict[str, Any]:
    """Trade's measure×period cross in miniature: a non-tab axis the data-driven
    signal flags as a meta-axis (a 単位 string member among numeric ones), whose
    members also carry a code hierarchy (@level/@parentCode) — 合計 measures
    (level 1) over monthly children (level 2). The classifier calls it a
    meta-axis, but flat-pivoting would spread the period dimension into columns,
    so auto must route it to Layer D rather than fold it (#34 flatness gate).
    """
    return {
        "GET_STATS_DATA": {
            "RESULT": {"STATUS": 0},
            "STATISTICAL_DATA": {
                "RESULT_INF": {"TOTAL_NUMBER": 4},
                "TABLE_INF": {"@id": "T"},
                "CLASS_INF": {"CLASS_OBJ": [
                    {"@id": "cat02", "@name": "数量・金額", "CLASS": [
                        {"@code": "120", "@name": "合計_数量", "@level": "1"},
                        {"@code": "140", "@name": "合計_金額", "@level": "1", "@unit": "千円"},
                        {"@code": "100", "@name": "単位", "@level": "1"},
                        {"@code": "150", "@name": "1月_数量", "@level": "2", "@parentCode": "120"},
                    ]},
                    {"@id": "time", "@name": "時間軸（年次）",
                     "CLASS": {"@code": "2020000000", "@name": "2020年"}},
                ]},
                "DATA_INF": {"VALUE": [
                    {"@cat02": "120", "@time": "2020000000", "$": "5"},
                    {"@cat02": "140", "@time": "2020000000", "$": "1000"},
                    {"@cat02": "100", "@time": "2020000000", "$": "ＮＯ"},
                    {"@cat02": "150", "@time": "2020000000", "$": "2"},
                ]},
            },
        }
    }


class TestRawMode:
    """``rule=None`` must hand back what e-Stat returned, only flattened."""

    def test_returns_axis_id_keyed_rows_unchanged(self) -> None:
        client = _make_client(_population_payload())
        resp = client.get_stats_data("0003448237", rule=None)
        assert resp.values[0] == {
            "tab": "020",
            "cat01": "000",
            "time": "2020000000",
            "unit": "千人",
            "value": "126146",
        }


class TestAutoMode:
    """``rule="auto"`` classifies the table and resolves through Layers
    C > B > A > D. These pin the endpoint wiring: a matching v2 rule wins,
    a clean unmatched table gets a Layer A generic rule, and a table that
    cannot be structured falls to Layer D."""

    def test_auto_uses_layer_a_generic_when_no_rule_matches(self) -> None:
        # No v2 rules supplied; the fixture is a clean value+category+time
        # table, so Layer A builds a generic rule. Output is canonical cells
        # (#35): a time object, a {code,label} category, and a {value,unit}
        # measure — the cell left uncoerced (Layer A never casts).
        client = _make_client(_population_payload(), builtin_rules=[])
        resp = client.get_stats_data("0003448237")
        assert resp.values[0] == {
            "time": {"code": "2020000000", "label": "2020年",
                     "normalized": "2020", "granularity": "yearly"},
            "cat01": {"code": "000", "label": "男女計"},
            "value": {"value": "126146", "unit": "千人"},
        }

    def test_auto_applies_matching_v2_rule(self) -> None:
        # A v2 rule whose role_pattern matches the classified table is
        # selected over the generic. Its explicit yearly transform and the
        # column name "year" (which Layer A would never emit) are the signal
        # that the rule, not the generic default, ran.
        builtin = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["value", "category", "time"]},
            "output": [
                {"column": "year", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload(), builtin_rules=[builtin])
        resp = client.get_stats_data("0003448237")
        assert resp.values[0] == {
            "year": {"code": "2020000000", "label": "2020年",
                     "normalized": "2020", "granularity": "yearly"},
            "value": {"value": "126146", "unit": "千人"},
        }

    def test_auto_pivots_meta_axis_table_without_a_builtin(self) -> None:
        # #34: a single-meta-axis table with no matching builtin no longer
        # falls to Layer D — Layer A auto-generates a pivot rule, so auto folds
        # the two spread rows into one record keyed by the meta-member names.
        client = _make_client(_meta_axis_payload(), builtin_rules=[])
        out = client.get_stats_data("X").values
        assert len(out) == 1
        assert out[0]["time"]["normalized"] == "2020"
        assert out[0]["数量"] == {"value": "5", "unit": None}
        assert out[0]["金額"] == {"value": "1000", "unit": None}

    def test_auto_does_not_pivot_a_hierarchical_meta_axis(self) -> None:
        # #34 flatness gate: a meta-axis carrying a code hierarchy folds a
        # second dimension into its members (trade's measure×period cross), so
        # auto declines the generic pivot and rides Layer D — rows stay spread
        # (one per member), data preserved, nothing flattened into columns.
        client = _make_client(_hierarchical_meta_axis_payload(), builtin_rules=[])
        out = client.get_stats_data("X").values
        assert len(out) == 4  # one row per member — not folded into one record
        # Layer D shape: the meta-axis is a {code,label} dimension, not columns.
        assert out[0]["cat02"] == {"code": "120", "label": "合計_数量"}
        assert out[0]["value"] == {"value": "5", "unit": None}
        assert out[0]["time"]["normalized"] == "2020"

    def test_auto_demotes_to_layer_d_when_matched_rule_cannot_bind(self) -> None:
        # A builtin matches the role pattern but references a role absent
        # from the table (area on an area-less table); apply_v2_rule raises
        # RoleResolutionError, and because the failing rule is library-supplied
        # (Layer B) the auto path demotes to Layer D instead of surfacing it
        # (#32). A user rule in the same spot would surface — see
        # TestAutoFailurePolicy.
        builtin = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["value", "category", "time"]},
            "output": [
                {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload(), builtin_rules=[builtin])
        row = client.get_stats_data("0003448237").values[0]
        assert row["tab"]["label"] == "総人口"  # Layer D output, not a crash
        assert row["time"]["normalized"] == "2020"
        assert row["value"]["value"] == "126146"


class TestAutoPivot:
    """A matched v2 pivot rule folds meta-axis-spread rows end-to-end.

    This pins the request-path wiring specific to #10: the endpoint must
    thread ``class_objs`` (the meta-member names a ``where`` predicate
    matches against) from the fetched page through ``apply_auto`` into the
    pivot. The ``_meta_axis_payload`` tab carries 数量 / 金額, so a builtin
    rule selecting both collapses its two rows into one record.
    """

    def test_auto_applies_matching_pivot_rule(self) -> None:
        pivot = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["meta-axis", "time"]},
            "output": [
                {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "quantity", "source": {"role": "meta-axis", "where": {"equals": "数量"}}},
                {"column": "amount", "source": {"role": "meta-axis", "where": {"equals": "金額"}}},
            ],
        })
        client = _make_client(_meta_axis_payload(), builtin_rules=[pivot])
        out = client.get_stats_data("X").values
        assert out == ({
            "time": {"code": "2020000000", "label": "2020年",
                     "normalized": "2020", "granularity": "yearly"},
            "quantity": {"value": "5", "unit": None},
            "amount": {"value": "1000", "unit": None},
        },)

    def test_builtin_pivot_that_cannot_bind_demotes_to_layer_d(self) -> None:
        # A builtin (Layer B) pivot rule that takes the pivot path (it has a
        # `where` column) but cannot bind one of its non-meta columns — here
        # an `area` column on a meta+time table with no area axis raises
        # RoleResolutionError inside the pivot. The auto path must demote to
        # Layer D (raw rows preserved), never surfacing the failure of a
        # library-supplied rule.
        broken = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["meta-axis", "time"]},
            "output": [
                {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "quantity", "source": {"role": "meta-axis", "where": {"equals": "数量"}}},
                # area role is absent from this meta+time table → raises while
                # the pivot binds its non-meta columns
                {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_meta_axis_payload(), builtin_rules=[broken])
        row = client.get_stats_data("X").values[0]
        # Layer D output (canonical cells, raw cell preserved), not a crash.
        assert row["tab"]["label"] == "数量"
        assert row["time"]["normalized"] == "2020"
        assert row["value"]["value"] == "5"


class TestAutoFailurePolicy:
    """``rule="auto"`` routes a rule-application failure by provenance (#32):
    a caller-authored rule (``user_rules``, Layer C) surfaces the typed error
    so the caller can fix it; a library-supplied rule (built-in, Layer B)
    degrades to Layer D, since the caller cannot fix it and preserved data
    beats a crash. ``docs/DESIGN.md`` Decision B is the source of truth.

    The population fixture classifies as ``value + category + time``; each
    rule below matches that pattern so resolution selects it, and the failure
    happens at apply time — the seam the policy governs.
    """

    _PATTERN = ["value", "category", "time"]

    def test_surfaces_user_rule_that_cannot_bind(self) -> None:
        # A user rule references `area`, absent from this area-less table.
        # Because the caller wrote it, the failure surfaces (contrast the
        # builtin in TestAutoMode, which demotes to Layer D).
        user = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": self._PATTERN},
            "output": [{"column": "area", "source": {"role": "area"}, "transform": "passthrough"}],
        })
        client = _make_client(_population_payload(), builtin_rules=[], user_rules=[user])
        with pytest.raises(RoleResolutionError, match="area"):
            client.get_stats_data("0003448237")

    def test_surfaces_unknown_transform_in_user_rule(self) -> None:
        # A typo'd transform in a caller-authored rule surfaces as a typed
        # UnknownTransformError, not a stray KeyError, so the caller can fix it.
        user = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": self._PATTERN},
            "output": [
                {"column": "year", "source": {"role": "time"}, "transform": "yrealy"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload(), builtin_rules=[], user_rules=[user])
        with pytest.raises(UnknownTransformError, match="year"):
            client.get_stats_data("0003448237")

    def test_degrades_unknown_transform_in_builtin_to_layer_d(self) -> None:
        # The same typo in a built-in rule is the library's bug, not the
        # caller's; auto degrades to Layer D (raw cell preserved) rather than
        # crashing the caller with an error they have no power to resolve.
        builtin = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": self._PATTERN},
            "output": [
                {"column": "year", "source": {"role": "time"}, "transform": "yrealy"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload(), builtin_rules=[builtin])
        row = client.get_stats_data("0003448237").values[0]
        assert row["tab"]["label"] == "総人口"  # Layer D output, not a crash
        assert row["time"]["normalized"] == "2020"
        assert row["value"]["value"] == "126146"

    def test_surfaces_user_rule_with_mismatched_time_format(self) -> None:
        # A user rule declares monthly_e_stat, but the fixture's time codes are
        # yearly-shaped. The mismatch is a TimeFormatError; because the caller
        # authored the rule, it surfaces so they can pick the right format —
        # not a silently best-efforted (and wrong) result they couldn't trace.
        user = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": self._PATTERN},
            "output": [
                {"column": "t", "source": {"role": "time"}, "transform": "monthly_e_stat"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload(), builtin_rules=[], user_rules=[user])
        with pytest.raises(TimeFormatError, match="monthly_e_stat"):
            client.get_stats_data("0003448237")

    def test_degrades_builtin_with_mismatched_time_format_to_layer_d(self) -> None:
        # The same mismatched format in a built-in is the library's problem,
        # not the caller's; auto degrades to Layer D (best-effort time, raw
        # cell preserved) rather than crashing the caller.
        builtin = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": self._PATTERN},
            "output": [
                {"column": "t", "source": {"role": "time"}, "transform": "monthly_e_stat"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload(), builtin_rules=[builtin])
        row = client.get_stats_data("0003448237").values[0]
        assert row["tab"]["label"] == "総人口"  # Layer D output, not a crash
        assert row["time"]["normalized"] == "2020"
        assert row["value"]["value"] == "126146"

    def test_degrades_conflicting_builtins_instead_of_crashing(self) -> None:
        # Two built-ins claiming one role pattern is a library packaging bug;
        # the auto path must not crash the caller with AmbiguousRuleError. It
        # skips the conflicted builtin layer and falls through to a Layer A
        # generic rule for this clean value+category+time table.
        def _dup(column: str) -> RuleV2:
            return RuleV2.model_validate({
                "schema_version": "2",
                "match": {"role_pattern": self._PATTERN},
                "output": [{"column": column, "source": {"role": "value"}, "transform": "passthrough"}],
            })

        client = _make_client(_population_payload(), builtin_rules=[_dup("a"), _dup("b")])
        row = client.get_stats_data("0003448237").values[0]
        assert row == {
            "time": {"code": "2020000000", "label": "2020年",
                     "normalized": "2020", "granularity": "yearly"},
            "cat01": {"code": "000", "label": "男女計"},
            "value": {"value": "126146", "unit": "千人"},
        }


class TestHeuristicMode:
    """``rule="heuristic"`` invokes Layer D directly, bypassing the
    resolution chain so the output is predictable regardless of which
    builtins ship."""

    def test_heuristic_does_not_consult_builtin_rules(self) -> None:
        # Even on a payload a bundled rule would match, ``"heuristic"``
        # must skip rule resolution — useful when a caller wants a
        # stable shape across pyestat versions.
        client = _make_client(_payload_with_area(_population_payload()))
        resp = client.get_stats_data("0003448237", rule="heuristic")
        row = resp.values[0]
        assert row["tab"]["label"] == "総人口"
        # Layer D still normalizes the time axis best-effort (the patched
        # fixture uses a monthly code) but does not cast the value.
        assert row["time"]["normalized"] == "2022-01"
        assert row["time"]["granularity"] == "monthly"
        assert row["value"]["value"] == "126146"


class TestExplicitRule:
    """An explicit ``RuleV2`` bypasses resolution and is applied against the
    request-path classification (computed lazily, since the endpoint only
    classifies up front for ``"auto"``)."""

    def test_applies_explicit_v2_rule(self) -> None:
        # The declared columns shape the output; resolution is skipped, so
        # the rule's role_pattern is irrelevant. This also pins the lazy
        # classify path apply_rule uses for an explicit RuleV2.
        rule = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["value", "category", "time"]},
            "output": [
                {"column": "yr", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload())
        resp = client.get_stats_data("0003448237", rule=rule)
        assert resp.values[0] == {
            "yr": {"code": "2020000000", "label": "2020年",
                   "normalized": "2020", "granularity": "yearly"},
            "value": {"value": "126146", "unit": "千人"},
        }

    def test_explicit_rule_that_cannot_bind_surfaces_typed_error(self) -> None:
        # An explicit rule is caller-authored: a binding failure surfaces as a
        # typed EstatError (no resolution chain, no Layer D demotion) so the
        # caller can fix the rule they passed (#32). Here `area` is absent from
        # the area-less population table.
        rule = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["value", "category", "time"]},
            "output": [{"column": "area", "source": {"role": "area"}, "transform": "passthrough"}],
        })
        client = _make_client(_population_payload())
        with pytest.raises(RoleResolutionError, match="area"):
            client.get_stats_data("0003448237", rule=rule)


class TestFlatProjection:
    """``StatsDataResponse.to_flat()`` projects the nested canonical values to
    the legacy flat suffix convention, so pandas users keep one column per
    field. The nested form stays the single source of truth; flat is a view
    (``pandas.DataFrame(resp.to_flat())``)."""

    def test_auto_nested_flattens_to_legacy_suffix_columns(self) -> None:
        # The Layer A generic auto output (nested) flattens to the familiar
        # cat01 / cat01_label, time / time_code / time_label /
        # time_granularity, and value / unit columns.
        client = _make_client(_population_payload(), builtin_rules=[])
        flat = client.get_stats_data("0003448237").to_flat()[0]
        assert flat == {
            "time": "2020",
            "time_code": "2020000000",
            "time_label": "2020年",
            "time_granularity": "yearly",
            "cat01": "000",
            "cat01_label": "男女計",
            "value": "126146",
            "unit": "千人",
        }

    def test_raw_response_to_flat_is_unchanged(self) -> None:
        # rule=None rows are already flat, so to_flat is a no-op — the method
        # is safe to call on any response shape.
        client = _make_client(_population_payload())
        resp = client.get_stats_data("0003448237", rule=None)
        assert resp.to_flat() == resp.values


class TestUserRules:
    """``user_rules`` / ``builtin_rules`` feed the v2 resolver's layers.

    The endpoint must wire them so user (C) shadows builtin (B) on the same
    role pattern, and an unrelated user rule still lets the builtin fire.
    The behaviors pinned here:

    * a user rule shadows a builtin matching the same role pattern;
    * a user rule for a different pattern does not block the builtin;
    * the kwarg is optional and defaults to "no user rules".

    The single output column's name is the observable signal of which
    layer won.
    """

    def _v2(self, *, role_pattern: list[str], column: str) -> RuleV2:
        return RuleV2.model_validate(
            {
                "schema_version": "2",
                "match": {"role_pattern": role_pattern},
                "output": [
                    {"column": column, "source": {"role": "value"}, "transform": "passthrough"},
                ],
            }
        )

    def test_user_rule_shadows_matching_builtin(self) -> None:
        # Both layers match the table's role pattern; the user layer wins,
        # so its column name is what surfaces.
        pattern = ["value", "category", "time"]
        client = _make_client(
            _population_payload(),
            builtin_rules=[self._v2(role_pattern=pattern, column="from_builtin")],
            user_rules=[self._v2(role_pattern=pattern, column="from_user")],
        )
        assert set(client.get_stats_data("0003448237").values[0]) == {"from_user"}

    def test_unrelated_user_rule_does_not_block_builtin(self) -> None:
        # A user rule for a different role pattern must not prevent the
        # builtin from firing on the table at hand; otherwise one user rule
        # would disable every bundled rule.
        client = _make_client(
            _population_payload(),
            builtin_rules=[self._v2(role_pattern=["value", "category", "time"], column="from_builtin")],
            user_rules=[self._v2(role_pattern=["time", "area", "value"], column="from_user")],
        )
        assert set(client.get_stats_data("0003448237").values[0]) == {"from_builtin"}

    def test_omitted_user_rules_preserves_default_behavior(self) -> None:
        # Smoke test: not passing user_rules must behave identically to the
        # builtin-only construction.
        client = _make_client(
            _population_payload(),
            builtin_rules=[self._v2(role_pattern=["value", "category", "time"], column="from_builtin")],
        )
        assert set(client.get_stats_data("0003448237").values[0]) == {"from_builtin"}
