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
* ``rule=Rule(...)`` — a v1 rule applied directly (the escape hatch,
  removed with v1 in #30), bypassing resolution.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from pyestat._endpoint import EstatClient
from pyestat._http import EstatHttpClient
from pyestat._engine.rule import Rule, RuleV2


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


def _rule(*, format: str = "yearly", time_id: str = "time") -> Rule:
    return Rule.model_validate(
        {
            "schema_version": "1",
            "match": {"statsCode": "00200524"},
            "axes": {"time": {"id": time_id, "format": format}},
            "value": {"type": "number"},
        }
    )


def _meta_axis_payload() -> dict[str, Any]:
    """A table whose ``tab`` axis carries several value types — a meta-axis.

    Folding it into one record needs the #10 pivot, which this MVP lacks, so
    ``rule="auto"`` cannot structure it generically and must route to
    Layer D. Built inline (not an on-disk fixture) because its whole point
    is a shape the bundled fixtures deliberately avoid.
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
        # table, so Layer A builds a generic rule — time normalized by the
        # role-default, category passed through, and the cell left uncoerced
        # (Layer A never casts).
        client = _make_client(_population_payload(), builtin_rules=[])
        resp = client.get_stats_data("0003448237")
        assert resp.values[0] == {"time": "2020", "cat01": "000", "value": "126146"}

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
        assert resp.values[0] == {"year": "2020", "value": "126146"}

    def test_auto_falls_to_layer_d_when_table_cannot_be_structured(self) -> None:
        # A multi-value-type tab axis is a meta-axis needing the #10 pivot;
        # with no pivot, auto routes to Layer D — best-effort time, additive
        # labels, raw codes and cell preserved, nothing dropped.
        client = _make_client(_meta_axis_payload(), builtin_rules=[])
        row = client.get_stats_data("X").values[0]
        assert row["time"] == "2020"
        assert row["time_granularity"] == "yearly"
        assert row["tab_label"] == "数量"
        assert row["value"] == "5"

    def test_auto_demotes_to_layer_d_when_matched_rule_cannot_bind(self) -> None:
        # A builtin matches the role pattern but references a role absent
        # from the table (area on an area-less table); apply_v2_rule raises
        # RoleResolutionError, and the auto path demotes to Layer D instead
        # of surfacing it — the "auto never errors" guarantee.
        builtin = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["value", "category", "time"]},
            "output": [
                {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
            ],
        })
        client = _make_client(_population_payload(), builtin_rules=[builtin])
        row = client.get_stats_data("0003448237").values[0]
        assert row["tab_label"] == "総人口"  # Layer D output, not a crash
        assert row["time"] == "2020"
        assert row["value"] == "126146"


class TestHeuristicMode:
    """``rule="heuristic"`` invokes Layer D directly, bypassing the
    resolution chain so the output is predictable regardless of which
    builtins ship."""

    def test_heuristic_does_not_consult_builtin_rules(self) -> None:
        # Even on a payload the bundled rule would match, ``"heuristic"``
        # must skip the rule manager — useful when a caller wants a
        # stable shape across pyestat versions.
        client = _make_client(_payload_with_area(_population_payload()))
        resp = client.get_stats_data("0003448237", rule="heuristic")
        row = resp.values[0]
        assert row["tab_label"] == "総人口"
        # Layer D still normalizes the time axis best-effort (the patched
        # fixture uses a monthly code) but does not cast the value.
        assert row["time"] == "2022-01"
        assert row["time_granularity"] == "monthly"
        assert row["value"] == "126146"


class TestExplicitRule:
    """An explicit rule bypasses resolution. A v1 ``Rule`` runs the legacy
    pipeline; a v2 ``RuleV2`` is applied against the request-path
    classification (computed lazily, since the endpoint only classifies up
    front for ``"auto"``)."""

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
        assert resp.values[0] == {"yr": "2020", "value": "126146"}

    def test_applies_time_normalizer_and_value_caster(self) -> None:
        # Pinning the full Transformer pipeline output for a yearly
        # interpretation of the fixture (its time code 2020000000 fits
        # the yearly_e_stat parser).
        client = _make_client(_population_payload())
        resp = client.get_stats_data("0003448237", rule=_rule(format="yearly"))
        row = resp.values[0]
        assert row["time"] == "2020"
        assert row["time_code"] == "2020000000"
        assert row["time_granularity"] == "yearly"
        assert row["value"] == 126146
        assert isinstance(row["value"], int)


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
