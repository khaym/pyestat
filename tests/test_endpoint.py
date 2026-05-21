"""Tests for the Layer-2 endpoint surface.

Layer 2 turns e-Stat's wire format into typed Python objects and walks
``NEXT_KEY`` pagination. The transport mechanics (retry, timeout, appId)
already have their own coverage in ``test_http_client.py`` and are
stubbed out here via ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pyestat._endpoint import (
    EstatClient,
    MetaInfoResponse,
    Page,
    StatsDataResponse,
    StatsListResponse,
)
from pyestat._http import EstatHttpClient, ProgressEvent
from pyestat.errors import EstatApiError, TooManyRowsError


FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestConstruction:
    def test_accepts_app_id_directly_for_caller_ergonomics(self) -> None:
        # Library users should not have to build an EstatHttpClient
        # manually just to get going; the canonical entrypoint is
        # ``EstatClient(app_id=...)``.
        client = EstatClient(app_id="test")
        assert client is not None

    def test_requires_either_app_id_or_http(self) -> None:
        with pytest.raises(ValueError, match="app_id or http"):
            EstatClient()


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _make_client(*responses: dict[str, Any]) -> tuple[EstatClient, list[httpx.Request]]:
    """Build an EstatClient whose transport replays the given JSON bodies in order.

    Returns the captured request log so a test can assert *which* URL /
    params the client used, not only what it received.
    """
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


# --- response models -------------------------------------------------------


class TestResponseModelsAreImmutable:
    """The typed responses are dataclasses; freezing them stops callers from
    mutating fields and accidentally relying on shared state across reads."""

    def test_stats_data_response_is_frozen(self) -> None:
        resp = StatsDataResponse(
            stats_data_id="x",
            total_number=0,
            table_inf={},
            class_objs=(),
            values=(),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            resp.stats_data_id = "y"  # type: ignore[misc]

    def test_page_is_frozen(self) -> None:
        page = Page(
            page_number=1,
            values=(),
            next_key=None,
            total_number=None,
            table_inf={},
            class_objs=(),
        )
        with pytest.raises(Exception):
            page.page_number = 2  # type: ignore[misc]


# --- response parsing ------------------------------------------------------


class TestParseStatsData:
    def test_parses_real_world_fixture(self) -> None:
        # The population fixture is small enough to assert end-to-end and
        # comes from an actual e-Stat response, so passing it proves the
        # parser handles the *real* @ / $ wire format, not a strawman.
        client, _ = _make_client(_load_fixture("get_stats_data_population_sample.json"))
        # ``rule=None`` keeps this test focused on the Layer-2 parse —
        # auto-mode label substitution is exercised in test_endpoint_rules.py.
        resp = client.get_stats_data("0003448237", rule=None)

        assert resp.stats_data_id == "0003448237"
        assert resp.total_number == 2
        assert resp.table_inf["@id"] == "0003448237"
        # @ prefix stripped, $ renamed to value
        assert resp.values[0] == {
            "tab": "020",
            "cat01": "000",
            "time": "2020000000",
            "unit": "千人",
            "value": "126146",
        }

    def test_raises_estat_api_error_on_logical_failure(self) -> None:
        # Non-zero RESULT.STATUS is e-Stat's way of saying "request was
        # transported fine but rejected"; that distinction matters because
        # retrying a logical error wastes the daily quota.
        client, _ = _make_client(_load_fixture("get_stats_data_error.json"))
        with pytest.raises(EstatApiError) as exc_info:
            client.get_stats_data("bad-id")
        assert exc_info.value.status == 100
        assert "該当データ" in exc_info.value.message

    def test_normalizes_single_value_dict_into_tuple(self) -> None:
        # e-Stat collapses a one-element VALUE array into a bare dict; if
        # we forget to renormalize, downstream code that does
        # ``for row in values`` silently iterates dict keys instead of rows.
        payload = {
            "GET_STATS_DATA": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "STATISTICAL_DATA": {
                    "RESULT_INF": {"TOTAL_NUMBER": 1},
                    "TABLE_INF": {"@id": "T"},
                    "CLASS_INF": {"CLASS_OBJ": []},
                    "DATA_INF": {
                        "VALUE": {"@tab": "A", "$": "42"}  # bare dict, not list
                    },
                },
            }
        }
        client, _ = _make_client(payload)
        resp = client.get_stats_data("T")
        assert resp.values == ({"tab": "A", "value": "42"},)

    def test_normalizes_single_class_obj_dict(self) -> None:
        # CLASS_OBJ also collapses to a dict when a table has only one
        # axis; rule fingerprinting iterates class_objs so this must
        # round-trip identically to the multi-axis case.
        payload = {
            "GET_STATS_DATA": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "STATISTICAL_DATA": {
                    "RESULT_INF": {"TOTAL_NUMBER": 0},
                    "TABLE_INF": {},
                    "CLASS_INF": {
                        "CLASS_OBJ": {  # bare dict instead of list
                            "@id": "time",
                            "@name": "時間軸",
                            "CLASS": {"@code": "2020", "@name": "2020年", "@level": "1"},
                        }
                    },
                    "DATA_INF": {"VALUE": []},
                },
            }
        }
        client, _ = _make_client(payload)
        resp = client.get_stats_data("T")
        assert len(resp.class_objs) == 1
        time_obj = resp.class_objs[0]
        assert time_obj.id == "time"
        assert time_obj.name == "時間軸"
        assert time_obj.classes == (
            {"code": "2020", "name": "2020年", "level": "1"},
        )


# --- pagination ------------------------------------------------------------


class TestPagination:
    def _page_payload(self, *, values: list[dict[str, str]], next_key: int | None, total: int) -> dict[str, Any]:
        result_inf: dict[str, Any] = {"TOTAL_NUMBER": total}
        if next_key is not None:
            result_inf["NEXT_KEY"] = next_key
        return {
            "GET_STATS_DATA": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "STATISTICAL_DATA": {
                    "RESULT_INF": result_inf,
                    "TABLE_INF": {"@id": "T"},
                    "CLASS_INF": {"CLASS_OBJ": []},
                    "DATA_INF": {"VALUE": values},
                },
            }
        }

    def test_follows_next_key_until_exhausted(self) -> None:
        # NEXT_KEY chaining is the only way to get past 100k rows on
        # ``getStatsData``; an off-by-one here would silently drop the
        # tail of every large table.
        client, captured = _make_client(
            self._page_payload(values=[{"@a": "1"}, {"@a": "2"}], next_key=3, total=4),
            self._page_payload(values=[{"@a": "3"}, {"@a": "4"}], next_key=None, total=4),
        )
        resp = client.get_stats_data("T")
        assert len(resp.values) == 4
        assert resp.values[0]["a"] == "1"
        assert resp.values[3]["a"] == "4"
        # second request must carry startPosition; otherwise we'd loop
        # on page 1 forever.
        assert captured[1].url.params["startPosition"] == "3"

    def test_progress_callback_fires_per_page(self) -> None:
        # Progress callbacks exist so a long fetch is observable; if we
        # forget to invoke it on the last page, a tqdm bar would freeze
        # at 99 percent.
        client, _ = _make_client(
            self._page_payload(values=[{"@a": "1"}], next_key=2, total=2),
            self._page_payload(values=[{"@a": "2"}], next_key=None, total=2),
        )
        events: list[ProgressEvent] = []
        client.get_stats_data("T", progress=events.append)
        assert [(e.page, e.rows_fetched, e.rows_total) for e in events] == [
            (1, 1, 2),
            (2, 2, 2),
        ]
        # total_pages is derivable from total_number / first-page size
        assert events[0].total_pages == 2

    def test_iter_pages_yields_one_at_a_time(self) -> None:
        # The streaming entrypoint exists so a 3.8M-row table never
        # materializes fully — confirm the generator pauses between
        # HTTP calls instead of pre-fetching everything.
        client, captured = _make_client(
            self._page_payload(values=[{"@a": "1"}], next_key=2, total=2),
            self._page_payload(values=[{"@a": "2"}], next_key=None, total=2),
        )
        pages = client.iter_stats_data_pages("T")
        first = next(pages)
        assert first.page_number == 1
        assert len(captured) == 1  # second request not yet issued
        second = next(pages)
        assert second.page_number == 2
        assert len(captured) == 2


# --- max_rows guard --------------------------------------------------------


class TestMaxRowsGuard:
    def test_raises_too_many_rows_before_fetching_data(self) -> None:
        # The guard exists so a curious caller doesn't accidentally pull
        # the 3.8M-row trade table; if cntGetFlg=Y said "too many", no
        # data page should ever be requested.
        count_payload = {
            "GET_STATS_DATA": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "STATISTICAL_DATA": {
                    "RESULT_INF": {"TOTAL_NUMBER": 3_828_581},
                },
            }
        }
        client, captured = _make_client(count_payload)
        with pytest.raises(TooManyRowsError) as exc:
            client.get_stats_data("0004049306", max_rows=100_000)
        assert exc.value.total == 3_828_581
        assert exc.value.limit == 100_000
        # exactly one HTTP call (the count probe); no data page requested
        assert len(captured) == 1
        assert captured[0].url.params["cntGetFlg"] == "Y"

    def test_passes_through_when_under_limit(self) -> None:
        # Under the cap the guard must let the fetch proceed; otherwise
        # max_rows would be a foot-gun rather than a safety net.
        count_payload = {
            "GET_STATS_DATA": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "STATISTICAL_DATA": {"RESULT_INF": {"TOTAL_NUMBER": 2}},
            }
        }
        data_payload = _load_fixture("get_stats_data_population_sample.json")
        client, _ = _make_client(count_payload, data_payload)
        resp = client.get_stats_data("0003448237", max_rows=10)
        assert len(resp.values) == 2

    def test_max_rows_none_skips_count_probe(self) -> None:
        # ``max_rows=None`` is documented as "commit to fetching
        # everything"; pre-flighting cntGetFlg in that case would burn
        # an extra round-trip per call for nothing.
        data_payload = _load_fixture("get_stats_data_population_sample.json")
        client, captured = _make_client(data_payload)
        client.get_stats_data("0003448237", max_rows=None)
        assert len(captured) == 1
        assert "cntGetFlg" not in captured[0].url.params


# --- meta info -------------------------------------------------------------


class TestGetMetaInfo:
    def test_parses_meta_info(self) -> None:
        # getMetaInfo is the cheap probe rule-fingerprint matching uses
        # before committing to a 3.8M-row download; the parsed shape
        # must surface axis @id and @name without requiring a data fetch.
        payload = {
            "GET_META_INFO": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "PARAMETER": {"STATS_DATA_ID": "T"},
                "METADATA_INF": {
                    "TABLE_INF": {"@id": "T", "STATISTICS_NAME": "x"},
                    "CLASS_INF": {
                        "CLASS_OBJ": [
                            {"@id": "tab", "@name": "表章項目", "CLASS": []},
                            {"@id": "time", "@name": "時間軸", "CLASS": []},
                        ]
                    },
                },
            }
        }
        client, _ = _make_client(payload)
        meta = client.get_meta_info("T")
        assert isinstance(meta, MetaInfoResponse)
        assert [c.id for c in meta.class_objs] == ["tab", "time"]

    def test_raises_estat_api_error(self) -> None:
        payload = {
            "GET_META_INFO": {
                "RESULT": {"STATUS": 100, "ERROR_MSG": "該当データなし"},
            }
        }
        client, _ = _make_client(payload)
        with pytest.raises(EstatApiError):
            client.get_meta_info("bad")


# --- list stats ------------------------------------------------------------


class TestListStats:
    def test_passes_search_params_through(self) -> None:
        # ``list_stats`` is the table-discovery entrypoint; its parameters
        # are forwarded raw because the e-Stat search API has many fields
        # (statsCode, searchWord, surveyYears, ...) and modeling each in
        # Python would just add a translation layer that lags behind the
        # API documentation.
        payload = {
            "GET_STATS_LIST": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "DATALIST_INF": {
                    "RESULT_INF": {"TOTAL_NUMBER": 1},
                    "TABLE_INF": [{"@id": "T1", "STATISTICS_NAME": "x"}],
                },
            }
        }
        client, captured = _make_client(payload)
        resp = client.list_stats(searchWord="人口", statsCode="00200524")
        assert isinstance(resp, StatsListResponse)
        assert resp.total_number == 1
        assert resp.tables[0]["@id"] == "T1"
        assert captured[0].url.params["searchWord"] == "人口"
        assert captured[0].url.params["statsCode"] == "00200524"

    def test_normalizes_single_table_into_tuple(self) -> None:
        # Same single-vs-list quirk seen in DATA_INF.VALUE; if a search
        # returns exactly one table, e-Stat hands back a dict instead of
        # a one-element list.
        payload = {
            "GET_STATS_LIST": {
                "RESULT": {"STATUS": 0, "ERROR_MSG": ""},
                "DATALIST_INF": {
                    "RESULT_INF": {"TOTAL_NUMBER": 1},
                    "TABLE_INF": {"@id": "Solo"},  # bare dict
                },
            }
        }
        client, _ = _make_client(payload)
        resp = client.list_stats()
        assert len(resp.tables) == 1
        assert resp.tables[0]["@id"] == "Solo"
