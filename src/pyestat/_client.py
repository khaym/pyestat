"""Synchronous HTTP client for the e-Stat API."""
from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel


DEFAULT_BASE_URL = "https://api.e-stat.go.jp/rest/3.0/app/json"
APP_ID_ENV_VAR = "ESTAT_APP_ID"


class EstatApiError(Exception):
    """Raised when e-Stat returns RESULT.STATUS != 0.

    e-Stat reports logical errors with an HTTP 200 status and a non-zero
    RESULT.STATUS in the JSON body, so transport-level success does not
    imply a successful query.
    """

    def __init__(self, *, status: int, message: str) -> None:
        super().__init__(f"e-Stat API error (status={status}): {message}")
        self.status = status
        self.message = message


class GetStatsDataResponse(BaseModel):
    """Parsed result of GET /getStatsData."""

    status: int
    error_msg: str
    stats_data_id: str
    values: list[dict[str, str]]


def _flatten_value_entry(entry: dict[str, Any]) -> dict[str, str]:
    """Flatten one e-Stat VALUE entry.

    e-Stat encodes dimension codes as ``@``-prefixed keys and the cell
    value under the ``$`` key (a JSON residue of the original XML schema).
    The flattened form drops the ``@`` prefix and renames ``$`` to
    ``value`` so consumers do not need to know the encoding.
    """
    result: dict[str, str] = {}
    for key, val in entry.items():
        if key.startswith("@"):
            result[key[1:]] = val
        elif key == "$":
            result["value"] = val
        else:
            result[key] = val
    return result


class EstatClient:
    """Synchronous client for the e-Stat REST API (v3.0, JSON)."""

    def __init__(
        self,
        app_id: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved_app_id = app_id if app_id is not None else os.environ.get(APP_ID_ENV_VAR)
        if not resolved_app_id:
            raise ValueError(
                f"app_id is required: pass it explicitly or set the {APP_ID_ENV_VAR} environment variable."
            )
        self._app_id = resolved_app_id
        self._http = httpx.Client(base_url=base_url, transport=transport)

    def get_stats_data(self, *, stats_data_id: str) -> GetStatsDataResponse:
        """Fetch one statistical table by its statsDataId."""
        response = self._http.get(
            "/getStatsData",
            params={"appId": self._app_id, "statsDataId": stats_data_id},
        )
        response.raise_for_status()
        payload = response.json()["GET_STATS_DATA"]

        status = payload["RESULT"]["STATUS"]
        error_msg = payload["RESULT"]["ERROR_MSG"]
        if status != 0:
            raise EstatApiError(status=status, message=error_msg)

        raw_values = payload["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
        # e-Stat collapses single-row VALUE arrays to a bare object.
        if isinstance(raw_values, dict):
            raw_values = [raw_values]

        return GetStatsDataResponse(
            status=status,
            error_msg=error_msg,
            stats_data_id=payload["PARAMETER"]["STATS_DATA_ID"],
            values=[_flatten_value_entry(v) for v in raw_values],
        )
