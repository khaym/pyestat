"""Tests for the rule= integration into EstatClient.get_stats_data.

The transformation pipeline (Layer 3) plugs into the endpoint client
(Layer 2). Four behavior modes are tested here:

* ``rule=None`` — raw mode (axis_id-keyed dicts, no transformation).
* ``rule="auto"`` (default) — try the user > project > builtin
  resolution chain; fall back to Layer D (``"heuristic"``) if nothing
  matched.
* ``rule="heuristic"`` — Layer D fallback (#23): best-effort ``time``
  normalization plus additive labels, preserving raw data; bypasses
  builtins.
* ``rule=Rule(...)`` — full declared transformation, bypassing the
  resolution chain.

The matcher / transformer mechanics already have isolated coverage;
these tests prove the modes are wired correctly into the endpoint.
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
from pyestat._engine.rule import Rule


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
    """``rule="auto"`` walks the resolution chain (user > project > builtin)
    and applies a matching rule; only when nothing matched does it
    fall back to Layer D (#23). This is the contract DESIGN.md Decision B
    / ARCHITECTURE.md Rule Resolution Order pin."""

    def test_auto_applies_builtin_when_one_matches(self) -> None:
        # The bundled population rule expects axes.area; with the area
        # axis present, ``rule="auto"`` should resolve to that rule
        # and produce normalized time / cast value fields. This is the
        # ergonomic default the library promises.
        client = _make_client(_payload_with_area(_population_payload()))
        resp = client.get_stats_data("0003448237")  # rule defaults to "auto"
        row = resp.values[0]
        assert row["time"] == "2022-01"
        assert row["time_code"] == "2022000101"
        assert row["time_granularity"] == "monthly"
        assert row["value"] == 126146
        assert isinstance(row["value"], int)

    def test_auto_falls_back_to_layer_d_when_no_builtin_matches(self) -> None:
        # The fixture's axes (no area) do not satisfy the bundled
        # population rule's FingerprintMatcher; ``"auto"`` then falls back
        # to Layer D.
        client = _make_client(_population_payload())
        resp = client.get_stats_data("0003448237")
        row = resp.values[0]
        # Layer D adds *_label fields AND normalizes the classifier-detected
        # time axis best-effort (the fixture's 2020000000 is yearly), but
        # never coerces the cell value (data preserved).
        assert row["tab_label"] == "総人口"
        assert row["cat01_label"] == "男女計"
        assert row["time"] == "2020"
        assert row["time_granularity"] == "yearly"
        assert row["time_code"] == "2020000000"
        assert row["value"] == "126146"  # still string — not cast


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
    """An explicit Rule bypasses the resolution chain too."""

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
    """``user_rules=`` is the ergonomic on-ramp for caller-defined rules.

    Decision E pins the chain user > project > builtin, but only the
    builtin layer is wired up automatically. ``user_rules`` lets callers
    inject Python ``Rule`` objects without touching the rule manager
    directly. The behaviors pinned here:

    * a user rule shadows a builtin that would otherwise match;
    * a non-matching user rule still lets the builtin chain fire;
    * the kwarg is optional and defaults to "no user rules".

    User and builtin rules are constructed identically apart from the
    fields under test; ``value.type`` (``"string"`` vs ``"number"``) is
    used as the observable signal of which layer won.
    """

    def _string_typed(self) -> Rule:
        return Rule.model_validate(
            {
                "schema_version": "1",
                "match": {"statsCode": "00200524"},
                "axes": {"time": {"id": "time", "format": "yearly"}},
                "value": {"type": "string"},
            }
        )

    def _number_typed(self) -> Rule:
        return Rule.model_validate(
            {
                "schema_version": "1",
                "match": {"statsCode": "00200524"},
                "axes": {"time": {"id": "time", "format": "yearly"}},
                "value": {"type": "number"},
            }
        )

    def test_user_rule_shadows_matching_builtin(self) -> None:
        # Both layers match the same table; the only observable
        # difference is value.type. If the user layer wins, the value
        # is cast to int; otherwise it stays as the e-Stat string.
        client = _make_client(
            _population_payload(),
            builtin_rules=[self._string_typed()],
            user_rules=[self._number_typed()],
        )
        resp = client.get_stats_data("0003448237")
        assert resp.values[0]["value"] == 126146
        assert isinstance(resp.values[0]["value"], int)

    def test_user_rule_does_not_block_unrelated_builtin(self) -> None:
        # A user rule scoped to a different statsCode must not prevent
        # the builtin chain from firing on the table at hand; otherwise
        # one user rule would disable every bundled rule.
        unrelated = Rule.model_validate(
            {
                "schema_version": "1",
                "match": {"statsCode": "99999999"},
                "axes": {"time": {"id": "time", "format": "yearly"}},
                "value": {"type": "number"},
            }
        )
        client = _make_client(
            _population_payload(),
            builtin_rules=[self._string_typed()],
            user_rules=[unrelated],
        )
        resp = client.get_stats_data("0003448237")
        # Builtin (value.type=string) wins because the user rule's
        # statsCode does not match.
        assert resp.values[0]["value"] == "126146"

    def test_omitted_user_rules_preserves_default_behavior(self) -> None:
        # Smoke test: not passing user_rules must behave identically to
        # the pre-existing client construction (builtin-only chain).
        client = _make_client(
            _population_payload(),
            builtin_rules=[self._number_typed()],
        )
        resp = client.get_stats_data("0003448237")
        assert resp.values[0]["value"] == 126146
