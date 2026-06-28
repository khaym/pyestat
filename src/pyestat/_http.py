"""Layer 1: HTTP transport for the e-Stat API.

Owns connection setup, timeout, and retry. Higher layers consume parsed
JSON dicts from this module and must not import ``httpx`` directly, so
an ``AsyncEstatHttpClient`` variant can be added later without touching
Layers 2-4 (see ARCHITECTURE.md).
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from pyestat._errors import HttpRetryExhaustedError


DEFAULT_BASE_URL = "https://api.e-stat.go.jp/rest/3.0/app/json"

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_BASE = 0.5

# HTTP status codes that indicate a transient condition worth retrying.
# 5xx are server-side; 408 and 429 are the only 4xx codes that signal
# "try again later" rather than "your request is malformed".
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProgressEvent:
    """Snapshot of multi-page fetch progress.

    Defined in Layer 1 because it is the shared contract between Layer 1
    (transport mechanics) and Layer 2 (which emits the event as each
    page arrives). ``total_pages`` / ``rows_total`` are ``None`` when
    the total has not yet been resolved (streaming / opt-out paths).
    """

    page: int
    total_pages: int | None
    rows_fetched: int
    rows_total: int | None


class EstatHttpClient:
    """Synchronous HTTP transport for the e-Stat REST API.

    Responsibilities are deliberately narrow: inject ``appId``, apply
    retry / timeout policy, and return the parsed JSON body. The client
    does NOT inspect ``RESULT.STATUS`` — that semantic check belongs to
    Layer 2 alongside the typed response models.
    """

    def __init__(
        self,
        *,
        app_id: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base: float = DEFAULT_RETRY_BACKOFF_BASE,
        retry_jitter: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not app_id:
            raise ValueError("app_id is required")
        self._app_id = app_id
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._retry_jitter = retry_jitter
        self._sleep = sleep
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=connect_timeout,
            pool=connect_timeout,
        )
        self._http = httpx.Client(
            base_url=base_url, transport=transport, timeout=timeout
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EstatHttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, path: str, *, params: Mapping[str, Any]) -> dict[str, Any]:
        """Issue a GET, retrying transient failures.

        Caller-supplied ``params`` win over the default ``appId`` so an
        operator script can swap tokens explicitly without subclassing.
        """
        merged: dict[str, Any] = {"appId": self._app_id, **params}
        last_status: int | None = None
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._http.get(path, params=merged)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_status = None
                last_exc = exc
                if attempt >= self._max_retries:
                    raise HttpRetryExhaustedError(
                        attempts=attempt, last_status=None, last_exc=exc
                    ) from exc
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code in _RETRYABLE_STATUS:
                last_status = response.status_code
                last_exc = None
                if attempt >= self._max_retries:
                    raise HttpRetryExhaustedError(
                        attempts=attempt,
                        last_status=response.status_code,
                        last_exc=None,
                    )
                self._sleep(self._backoff(attempt))
                continue

            response.raise_for_status()
            return response.json()

        raise HttpRetryExhaustedError(
            attempts=self._max_retries, last_status=last_status, last_exc=last_exc
        )

    def _backoff(self, attempt: int) -> float:
        base = self._retry_backoff_base * (2 ** (attempt - 1))
        if not self._retry_jitter:
            return base
        # Full jitter around the base delay: [0.5*base, 1.5*base).
        return base * (0.5 + random.random())
