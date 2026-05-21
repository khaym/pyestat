"""Tests for the rule= integration into EstatClient.get_stats_data.

The transformation pipeline (Layer 3) plugs into the endpoint client
(Layer 2). Three behavior modes are tested here:

* ``rule=None`` — raw mode (axis_id-keyed dicts, no transformation).
* ``rule="auto"`` (default) — heuristic, label substitution only.
* ``rule=Rule(...)`` — full declared transformation.

The matcher / transformer mechanics already have isolated coverage;
these tests prove the modes are wired correctly into the endpoint.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from pyestat._endpoint import EstatClient
from pyestat._http import EstatHttpClient
from pyestat._rule import Rule


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _make_client(payload: dict[str, Any]) -> EstatClient:
    queue: Iterator[dict[str, Any]] = iter([payload])
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=next(queue)))
    http = EstatHttpClient(app_id="x", transport=transport, sleep=lambda _s: None)
    return EstatClient(http=http)


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
        payload = json.loads((FIXTURE_DIR / "get_stats_data_population_sample.json").read_text(encoding="utf-8"))
        client = _make_client(payload)
        resp = client.get_stats_data("0003448237", rule=None)
        # Raw mode: same shape Layer 2 produces; no time/time_code/label
        # fields injected.
        assert resp.values[0] == {
            "tab": "020",
            "cat01": "000",
            "time": "2020000000",
            "unit": "千人",
            "value": "126146",
        }


class TestAutoMode:
    """``rule="auto"`` adds a ``{axis_id}_label`` for each axis that
    has a CLASS lookup, without any standard-code mapping or value
    typing — the safe-defaults behavior Decision B calls out."""

    def test_auto_is_the_default_mode(self) -> None:
        # The default makes the un-decorated call useful immediately;
        # a caller doing ``client.get_stats_data(id)`` should see
        # label-enriched rows out of the box.
        payload = json.loads((FIXTURE_DIR / "get_stats_data_population_sample.json").read_text(encoding="utf-8"))
        client = _make_client(payload)
        resp = client.get_stats_data("0003448237")
        # tab=020 → 総人口
        assert resp.values[0]["tab_label"] == "総人口"
        # cat01=000 → 男女計; cat01=001 → 男
        assert resp.values[0]["cat01_label"] == "男女計"
        assert resp.values[1]["cat01_label"] == "男"

    def test_auto_preserves_raw_axis_codes(self) -> None:
        # Auto mode adds labels alongside; it does not overwrite codes
        # because some downstream filters keep working on the raw code.
        payload = json.loads((FIXTURE_DIR / "get_stats_data_population_sample.json").read_text(encoding="utf-8"))
        client = _make_client(payload)
        resp = client.get_stats_data("0003448237", rule="auto")
        assert resp.values[0]["tab"] == "020"
        assert resp.values[0]["time"] == "2020000000"

    def test_auto_does_not_normalize_time_or_cast_value(self) -> None:
        # Auto stays "safe" by deferring any opinionated transformation
        # to an explicit rule. value stays a string; time stays the
        # raw 10-digit code.
        payload = json.loads((FIXTURE_DIR / "get_stats_data_population_sample.json").read_text(encoding="utf-8"))
        client = _make_client(payload)
        resp = client.get_stats_data("0003448237", rule="auto")
        assert resp.values[0]["value"] == "126146"
        assert "time_granularity" not in resp.values[0]


class TestExplicitRule:
    """An explicit Rule activates the full Transformer pipeline."""

    def test_applies_time_normalizer_and_value_caster(self) -> None:
        # The yearly population fixture has time code "2020000000";
        # under a yearly rule with value.type=number that becomes
        # {"time": "2020", "time_granularity": "yearly", "value": 126146}.
        payload = json.loads((FIXTURE_DIR / "get_stats_data_population_sample.json").read_text(encoding="utf-8"))
        client = _make_client(payload)
        resp = client.get_stats_data("0003448237", rule=_rule(format="yearly"))
        row = resp.values[0]
        assert row["time"] == "2020"
        assert row["time_code"] == "2020000000"
        assert row["time_granularity"] == "yearly"
        assert row["value"] == 126146
        assert isinstance(row["value"], int)
