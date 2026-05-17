"""Walking Skeleton tests for the getStatsData JSON endpoint.

Business rules verified here:
- A successful e-Stat response (RESULT.STATUS == 0) is returned as a typed model
  with the parsed status, the originating stats_data_id, and the data rows.
- Each VALUE entry in the response — whose keys are @-prefixed dimensions and
  whose value lives under "$" — is flattened into a plain dict: dimension codes
  become regular keys and the "$" payload becomes "value".
- A non-zero RESULT.STATUS surfaces as an EstatApiError carrying the status
  code and the server-provided error message, so callers can react to e-Stat's
  "HTTP 200 with logical error" convention.
- When the caller does not pass app_id, the client falls back to the
  ESTAT_APP_ID environment variable, and refuses to operate without one.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pyestat import EstatApiError, EstatClient


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _mock_client(response_body: dict, status_code: int = 200) -> EstatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_body)

    transport = httpx.MockTransport(handler)
    return EstatClient(app_id="test-app-id", transport=transport)


def test_get_stats_data_returns_typed_response_for_successful_request() -> None:
    body = _load_fixture("get_stats_data_population_sample.json")
    client = _mock_client(body)

    response = client.get_stats_data(stats_data_id="0003448237")

    assert response.status == 0
    assert response.error_msg == "正常に終了しました。"
    assert response.stats_data_id == "0003448237"
    assert len(response.values) == 2


def test_get_stats_data_flattens_at_and_dollar_keys_in_value_entries() -> None:
    body = _load_fixture("get_stats_data_population_sample.json")
    client = _mock_client(body)

    response = client.get_stats_data(stats_data_id="0003448237")

    first = response.values[0]
    assert first == {
        "tab": "020",
        "cat01": "000",
        "time": "2020000000",
        "unit": "千人",
        "value": "126146",
    }


def test_get_stats_data_raises_estat_api_error_on_nonzero_status() -> None:
    body = _load_fixture("get_stats_data_error.json")
    client = _mock_client(body)

    with pytest.raises(EstatApiError) as excinfo:
        client.get_stats_data(stats_data_id="nonexistent")

    assert excinfo.value.status == 100
    assert "該当データが存在しません" in excinfo.value.message


def test_client_uses_app_id_from_environment_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ESTAT_APP_ID", "env-app-id")
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["appId"] = request.url.params["appId"]
        return httpx.Response(
            200, json=_load_fixture("get_stats_data_population_sample.json")
        )

    client = EstatClient(transport=httpx.MockTransport(handler))
    client.get_stats_data(stats_data_id="0003448237")

    assert captured["appId"] == "env-app-id"


def test_client_refuses_to_initialize_without_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ESTAT_APP_ID", raising=False)

    with pytest.raises(ValueError, match="ESTAT_APP_ID"):
        EstatClient()
