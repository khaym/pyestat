"""Tests for the Layer-1 HTTP I/O surface.

Layer 1 is responsible for transport mechanics only:

* injecting ``appId``,
* honoring connect / read timeouts,
* retrying on transient failures (5xx, 408, 429, connection error, timeout),
* and returning the parsed JSON body to the layer above.

Anything about e-Stat business semantics (``RESULT.STATUS``, pagination,
typed responses) lives in Layer 2 and must not bleed into these tests.
"""
from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from pyestat._http import (
    DEFAULT_BASE_URL,
    EstatHttpClient,
    HttpRetryExhaustedError,
    ProgressEvent,
)


# --- helpers ---------------------------------------------------------------


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    app_id: str = "test-app-id",
    **kwargs: Any,
) -> EstatHttpClient:
    """Build an EstatHttpClient backed by an in-process MockTransport.

    Retry sleeps are stubbed out with ``sleep=lambda _s: None`` so the
    suite stays fast and deterministic — we are testing the *decision*
    to back off, not the wall-clock delay.
    """
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("sleep", lambda _s: None)
    return EstatHttpClient(app_id=app_id, transport=transport, **kwargs)


# --- construction ----------------------------------------------------------


class TestConstruction:
    def test_app_id_is_required(self) -> None:
        # The client cannot make any e-Stat call without an appId; failing
        # at construction time is friendlier than failing on first request.
        with pytest.raises(ValueError, match="app_id"):
            EstatHttpClient(app_id="")

    def test_default_base_url_points_at_official_v3_json_endpoint(self) -> None:
        # Anchoring the default base URL in a test makes accidental edits
        # to the endpoint version (v2 / v3, json / xml) visible in review.
        assert DEFAULT_BASE_URL == "https://api.e-stat.go.jp/rest/3.0/app/json"


# --- request shape ---------------------------------------------------------


class TestRequest:
    def test_appid_is_injected_into_every_request(self) -> None:
        # Callers should not have to remember to pass appId; the client
        # owns that secret and threads it through automatically.
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        client = _make_client(handler, app_id="my-token")
        client.request("/getStatsData", params={"statsDataId": "0003443838"})

        assert seen[0].url.params["appId"] == "my-token"
        assert seen[0].url.params["statsDataId"] == "0003443838"

    def test_returns_parsed_json_body(self) -> None:
        # Layer 1 hands back ``dict``; Layer 2 narrows that into typed
        # response models. Parsing JSON in L1 saves every caller from
        # repeating ``response.json()``.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"GET_STATS_DATA": {"RESULT": {"STATUS": 0}}})

        client = _make_client(handler)
        body = client.request("/getStatsData", params={"statsDataId": "x"})
        assert body == {"GET_STATS_DATA": {"RESULT": {"STATUS": 0}}}

    def test_caller_params_override_default_params(self) -> None:
        # If a caller passes ``appId`` explicitly (e.g. a script that
        # juggles multiple tokens), the explicit value wins — surprising
        # users by silently overwriting is worse than respecting intent.
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        client = _make_client(handler, app_id="default-token")
        client.request("/foo", params={"appId": "override-token"})
        assert captured[0].url.params["appId"] == "override-token"


# --- retry behavior --------------------------------------------------------


class TestRetry:
    def test_retries_on_5xx_then_succeeds(self) -> None:
        # 5xx is the canonical "server hiccup, try again" signal.
        responses = iter(
            [
                httpx.Response(503, json={"err": "down"}),
                httpx.Response(502, json={"err": "down"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )

        def handler(_: httpx.Request) -> httpx.Response:
            return next(responses)

        client = _make_client(handler)
        assert client.request("/x", params={}) == {"ok": True}

    def test_retry_exhausts_and_raises(self) -> None:
        # After ``max_retries`` failed attempts the client must surface
        # a typed error so callers can distinguish "down" from "logical
        # API error" (the latter is Layer 2's RESULT.STATUS check).
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="still down")

        client = _make_client(handler, max_retries=3)
        with pytest.raises(HttpRetryExhaustedError) as exc_info:
            client.request("/x", params={})
        # The error should expose the final attempt's status so logs are
        # actionable without re-running the request under a debugger.
        assert exc_info.value.last_status == 503
        assert exc_info.value.attempts == 3

    def test_retries_on_429_and_408(self) -> None:
        # 429 (rate limit) and 408 (request timeout) are the only transient
        # 4xx codes the client retries; everything else 4xx is the
        # caller's fault and must not be retried.
        for transient in (408, 429):
            responses = iter([httpx.Response(transient), httpx.Response(200, json={"ok": True})])

            def handler(_: httpx.Request) -> httpx.Response:
                return next(responses)

            client = _make_client(handler)
            assert client.request("/x", params={}) == {"ok": True}

    def test_does_not_retry_on_404(self) -> None:
        # Deterministic 4xx (404, 400, 401, 403…) won't change on retry
        # — retrying just wastes the e-Stat quota.
        calls = itertools.count()

        def handler(_: httpx.Request) -> httpx.Response:
            next(calls)
            return httpx.Response(404, text="not found")

        client = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client.request("/x", params={})
        assert next(calls) == 1  # exactly one attempt, no retries

    def test_retries_on_connect_error(self) -> None:
        # Connection-level failures (DNS, RST, TLS reset) are the most
        # common e-Stat hiccup observed in practice.
        attempts = itertools.count()

        def handler(request: httpx.Request) -> httpx.Response:
            n = next(attempts)
            if n < 2:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json={"ok": True})

        client = _make_client(handler)
        assert client.request("/x", params={}) == {"ok": True}

    def test_retries_on_read_timeout(self) -> None:
        # e-Stat is observed to be slow on large tables; a ReadTimeout
        # is "the server is still working" rather than "the server is
        # broken", so the standard backoff applies.
        attempts = itertools.count()

        def handler(request: httpx.Request) -> httpx.Response:
            n = next(attempts)
            if n < 1:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json={"ok": True})

        client = _make_client(handler)
        assert client.request("/x", params={}) == {"ok": True}

    def test_backoff_schedule_follows_doubled_base(self) -> None:
        # The client commits to a 0.5s → 1s → 2s backoff. We verify the *sequence*,
        # not the wall-clock delay, by capturing what the client asked
        # ``sleep`` to wait for.
        sleeps: list[float] = []

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = EstatHttpClient(
            app_id="x",
            transport=httpx.MockTransport(handler),
            max_retries=3,
            retry_backoff_base=0.5,
            retry_jitter=False,
            sleep=sleeps.append,
        )
        with pytest.raises(HttpRetryExhaustedError):
            client.request("/x", params={})

        # 3 attempts = 2 sleeps between them. Jitter is disabled so we
        # can assert the exact pattern.
        assert sleeps == [0.5, 1.0]


# --- progress event surface ------------------------------------------------


class TestProgressEvent:
    def test_progress_event_carries_pagination_state(self) -> None:
        # ProgressEvent is the contract between Layer 1 (where the type
        # lives) and Layer 2 (where it gets emitted). Pin the field set
        # so a Layer-2 change does not silently drop a field that a
        # caller's tqdm bridge depends on.
        evt = ProgressEvent(page=2, total_pages=5, rows_fetched=200_000, rows_total=500_000)
        assert (evt.page, evt.total_pages, evt.rows_fetched, evt.rows_total) == (2, 5, 200_000, 500_000)

    def test_progress_event_total_pages_can_be_unknown(self) -> None:
        # In streaming / no-cntGetFlg paths the total is not yet known;
        # ``None`` is the right "I don't know yet" signal.
        evt = ProgressEvent(page=1, total_pages=None, rows_fetched=100, rows_total=None)
        assert evt.total_pages is None
        assert evt.rows_total is None
