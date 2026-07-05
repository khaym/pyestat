"""Tests for ``EstatClient.explain_table``.

``explain_table`` is the authoring-time window into how pyestat *reads* a
table: the role pattern the classifier infers, each axis's role and confidence,
which resolution layer would cover it, and a proposed generic rule to hand-edit.
It fills the gap that the classifier is otherwise private, so a rule author (or
the authoring Skill) can learn a table's ``role_pattern`` — the key a
``RuleV2.match`` must equal — instead of guessing it.

It classifies from a sample of the table's data (its first page), the same
data-driven view ``rule="auto"`` uses. That matters because metadata alone
cannot separate a measure-spread ``meta-axis`` from a plain ``category``: an
axis merely *named* like a measure (数量 / 金額 / …) reads as a meta-axis from
metadata but as a category from data — a divergence a metadata-only report would
hand the author as a role pattern that then never matches. These tests pin the
data-driven behavior, including that empty data falls back to a metadata reading
rather than silently downgrading a meta-axis on zero observations.

The helper deliberately does *not* diagnose data hazards (mixed
calendar/fiscal years, aggregate rows): those are one of an open-ended set and
belong to the authoring dialog reading raw members, not to a fixed list baked
into the library.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from pyestat._endpoint import EstatClient, MetaInfoResponse
from pyestat._http import EstatHttpClient


def _make_client(*responses: dict[str, Any]) -> tuple[EstatClient, list[httpx.Request]]:
    captured: list[httpx.Request] = []
    queue: Iterator[dict[str, Any]] = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=next(queue))

    http = EstatHttpClient(
        app_id="test-id",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    return EstatClient(http=http), captured


def _axis(axis_id: str, name: str, members: list[tuple[str, ...]]) -> dict[str, Any]:
    """One ``CLASS_OBJ``. Each member is ``(code, name)``, ``(code, name, level)``,
    or ``(code, name, level, parentCode)``."""
    classes = []
    for m in members:
        entry: dict[str, Any] = {"@code": m[0], "@name": m[1]}
        if len(m) > 2:
            entry["@level"] = m[2]
        if len(m) > 3:
            entry["@parentCode"] = m[3]
        classes.append(entry)
    return {"@id": axis_id, "@name": name, "CLASS": classes}


def _meta(axes: list[dict[str, Any]], table_inf: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "GET_META_INFO": {
            "RESULT": {"STATUS": 0},
            "METADATA_INF": {
                "TABLE_INF": table_inf or {},
                "CLASS_INF": {"CLASS_OBJ": axes},
            },
        }
    }


def _data(
    axes: list[dict[str, Any]],
    values: list[dict[str, Any]],
    table_inf: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "GET_STATS_DATA": {
            "RESULT": {"STATUS": 0},
            "STATISTICAL_DATA": {
                "RESULT_INF": {"TOTAL_NUMBER": len(values), "NEXT_KEY": None},
                "TABLE_INF": table_inf or {},
                "CLASS_INF": {"CLASS_OBJ": axes},
                "DATA_INF": {"VALUE": values},
            },
        }
    }


# A CPI-shaped table: a ``tab`` value-type axis (→ meta-axis), a plain category,
# area, and a time axis mixing calendar/fiscal/monthly members.
CPI_AXES = [
    _axis("tab", "表章項目", [("1", "指数"), ("2", "前年比")]),
    _axis("cat01", "品目", [("0001", "総合"), ("0002", "食料")]),
    _axis("area", "地域", [("00000", "全国")]),
    _axis("time", "時間軸（年・月）", [
        ("2015000000", "2015年"),
        ("2015100000", "2015年度"),
        ("2016000606", "2016年6月"),
    ]),
]
CPI_ROWS = [
    {"@tab": "1", "@cat01": "0001", "@area": "00000", "@time": "2015000000", "$": "100"},
    {"@tab": "2", "@cat01": "0001", "@area": "00000", "@time": "2015000000", "$": "1.5"},
    {"@tab": "1", "@cat01": "0002", "@area": "00000", "@time": "2016000606", "$": "102"},
]

# A foreign-trade-shaped table: cat02 is a flat measure-spread meta-axis with a
# unit-string member (単位) among numeric ones.
TRADE_AXES = [
    _axis("cat01", "概況品目", [("00000000", "食料品")]),
    _axis("cat02", "数量・金額", [("100", "単位"), ("101", "合計_数量"), ("102", "合計_金額")]),
    _axis("area", "国", [("50103", "大韓民国")]),
    _axis("time", "時間軸(年次)", [("2026000000", "2026年")]),
]
TRADE_ROWS = [
    {"@cat01": "00000000", "@cat02": "100", "@area": "50103", "@time": "2026000000", "$": "ＮＯ"},
    {"@cat01": "00000000", "@cat02": "101", "@area": "50103", "@time": "2026000000", "$": "12345"},
    {"@cat01": "00000000", "@cat02": "102", "@area": "50103", "@time": "2026000000", "$": "98765"},
]
TRADE_STATS_CODE = {"STAT_NAME": {"@code": "00350300"}}


class TestExplainTable:
    def test_role_pattern_is_data_driven_and_tab_is_meta_axis(self) -> None:
        # Every call reads getMetaInfo + the first data page, so the report is
        # the data-driven classification the auto path also uses.
        client, captured = _make_client(_meta(CPI_AXES), _data(CPI_AXES, CPI_ROWS))

        exp = client.explain_table("0003427113")

        assert exp.role_pattern == ("meta-axis", "category", "area", "time")
        roles = exp.roles
        assert roles["tab"].role == "meta-axis"
        assert roles["tab"].confidence == "high"
        assert roles["time"].role == "time"
        assert roles["area"].role == "area"
        assert len(captured) == 2
        assert captured[0].url.path.endswith("/getMetaInfo")
        assert captured[1].url.path.endswith("/getStatsData")

    def test_returns_bundled_meta_and_roles_keyed_by_axis_id(self) -> None:
        # explain_table already fetches metadata internally, so it returns it as
        # ``meta``: an author reads the axes' member codes (for select or a rule)
        # from the same result, with no second get_meta_info round-trip. The
        # interpretation is ``roles``, keyed by the *same* axis id the facts use,
        # so ``exp.roles[k]`` and ``exp.meta.class_objs`` speak one vocabulary.
        client, captured = _make_client(_meta(CPI_AXES), _data(CPI_AXES, CPI_ROWS))

        exp = client.explain_table("0003427113")

        # facts: the metadata explain_table fetched, exposed for authoring
        assert isinstance(exp.meta, MetaInfoResponse)
        assert [co.id for co in exp.meta.class_objs] == ["tab", "cat01", "area", "time"]
        assert exp.meta.class_objs[1].classes[0]["code"] == "0001"  # a member code
        # interpretation keyed by the same ids as the facts (single vocabulary)
        assert set(exp.roles) == {co.id for co in exp.meta.class_objs}
        # AxisReading is role-only; id/name live on the facts, not duplicated here
        assert not hasattr(exp.roles["tab"], "axis_id")
        assert not hasattr(exp.roles["tab"], "name")
        # no extra metadata round-trip beyond the internal meta + one data page
        assert len(captured) == 2

    def test_measure_spread_meta_axis_reads_high_from_data(self) -> None:
        # cat02's unit-string-among-numerics split is confirmed from data, so it
        # is a high-confidence meta-axis (not the metadata lexicon guess).
        client, _ = _make_client(_meta(TRADE_AXES), _data(TRADE_AXES, TRADE_ROWS))

        exp = client.explain_table("0004049327")

        cat02 = exp.roles["cat02"]
        assert cat02.role == "meta-axis"
        assert cat02.confidence == "high"
        assert exp.role_pattern == ("category", "meta-axis", "area", "time")

    def test_lexicon_named_category_is_classified_category_not_meta_axis(self) -> None:
        # An income-bracket axis is *named* with 金額 (a measure word), so
        # metadata alone would call it a meta-axis. Its cells are all numeric
        # counts, so the data-driven reading correctly calls it a category —
        # exactly the false positive sampling exists to avoid, since a rule
        # authored against a spurious `meta-axis` pattern would never match.
        axes = [
            _axis("tab", "表章項目", [("1", "世帯数")]),
            _axis("cat01", "年間収入階級（金額）", [
                ("1", "200万円未満"), ("2", "200～400万円"), ("3", "400万円以上"),
            ]),
            _axis("area", "地域", [("00000", "全国")]),
            _axis("time", "時間軸(年次)", [("2020000000", "2020年")]),
        ]
        rows = [
            {"@tab": "1", "@cat01": "1", "@area": "00000", "@time": "2020000000", "$": "1200"},
            {"@tab": "1", "@cat01": "2", "@area": "00000", "@time": "2020000000", "$": "3400"},
            {"@tab": "1", "@cat01": "3", "@area": "00000", "@time": "2020000000", "$": "5600"},
        ]
        client, _ = _make_client(_meta(axes), _data(axes, rows))

        exp = client.explain_table("x")

        assert exp.roles["cat01"].role == "category"
        assert exp.role_pattern == ("value", "category", "area", "time")

    def test_coverage_is_builtin_when_a_builtin_rule_matches(self) -> None:
        # Trade's role pattern + statsCode matches the bundled foreign_trade
        # rule, so pyestat already covers it — no authoring needed.
        client, _ = _make_client(
            _meta(TRADE_AXES, TRADE_STATS_CODE),
            _data(TRADE_AXES, TRADE_ROWS, TRADE_STATS_CODE),
        )

        exp = client.explain_table("0004049327")

        assert exp.role_pattern == ("category", "meta-axis", "area", "time")
        assert exp.coverage == "builtin"

    def test_coverage_is_generic_with_a_proposed_rule(self) -> None:
        # No specific rule matches, but the structure is generic-friendly, so
        # the auto path structures it (Layer A) and a proposed rule is offered.
        client, _ = _make_client(_meta(CPI_AXES), _data(CPI_AXES, CPI_ROWS))

        exp = client.explain_table("x")

        assert exp.coverage == "generic"
        assert exp.proposed_rule is not None

    def test_coverage_is_fallback_and_no_rule_for_hierarchical_meta_axis(self) -> None:
        # cat02 folds a second dimension into its members (a code hierarchy):
        # a high-confidence meta-axis, but not flat-pivotable, so it rides the
        # lossless fallback and no generic rule can be proposed.
        axes = [
            _axis("cat01", "概況品目", [("00000000", "食料品")]),
            _axis("cat02", "数量・金額×月", [
                ("100", "単位"),
                ("200", "合計", "1"),
                ("201", "合計_数量", "2", "200"),
                ("202", "合計_金額", "2", "200"),
            ]),
            _axis("area", "国", [("50103", "大韓民国")]),
            _axis("time", "時間軸(年次)", [("2026000000", "2026年")]),
        ]
        rows = [
            {"@cat01": "00000000", "@cat02": "100", "@area": "50103", "@time": "2026000000", "$": "ＮＯ"},
            {"@cat01": "00000000", "@cat02": "201", "@area": "50103", "@time": "2026000000", "$": "123"},
            {"@cat01": "00000000", "@cat02": "202", "@area": "50103", "@time": "2026000000", "$": "456"},
        ]
        client, _ = _make_client(_meta(axes), _data(axes, rows))

        exp = client.explain_table("x")

        cat02 = exp.roles["cat02"]
        assert cat02.role == "meta-axis"
        assert cat02.confidence == "high"
        assert exp.coverage == "fallback"
        assert exp.proposed_rule is None

    def test_empty_data_falls_back_to_metadata_reading(self) -> None:
        # An empty first page means zero observations. Classifying an empty
        # profile would demote a measure-spread meta-axis to category on no
        # evidence, so explain_table reads from metadata instead — cat02 stays a
        # (medium, lexicon-inferred) meta-axis rather than flipping to category.
        client, captured = _make_client(
            _meta(TRADE_AXES, TRADE_STATS_CODE),
            _data(TRADE_AXES, [], TRADE_STATS_CODE),
        )

        exp = client.explain_table("x")

        cat02 = exp.roles["cat02"]
        assert cat02.role == "meta-axis"
        assert cat02.confidence == "medium"  # lexicon inference, not data
        assert len(captured) == 2  # still attempted the data fetch
