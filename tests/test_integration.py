"""Integration tests against the live e-Stat API.

Skipped when ``ESTAT_APP_ID`` is unset so a contributor without a
token still gets a green ``pytest`` run on the unit suite alone.

These tests are marked ``integration`` (registered in pyproject.toml).
Run only the unit tests with ``pytest -m "not integration"``; run only
the live-API tests with ``pytest -m integration``.

Coverage focus:

* The benchmark tables come back structured from the live API: GDP and
  the population estimates fold through the generic Layer A path,
  which needs no bundled rule, and their time axis is normalized with the
  right granularity. The one bundled rule — foreign trade — is not
  exercised here: the table is 3.8M rows and excluded by ``max_rows`` (it
  is pinned on synthetic rows in ``tests/test_builtin_rules.py`` instead).
* The ``max_rows`` guard refuses an oversized table without paying
  to download it.
* Raw mode still works against live JSON.
* Progress callbacks observe at least one page.
* ``list_stats`` returns search results.

Pagination over many pages is intentionally not exercised here — the
two small benchmark tables fit in one page each, and the trade table
is excluded by ``max_rows``. The NEXT_KEY walk is already covered by
``tests/test_endpoint.py``.
"""
from __future__ import annotations

import os

import pytest

from pyestat import EstatClient, ProgressEvent, TooManyRowsError


pytestmark = pytest.mark.integration


# Concrete statsDataId values verified against the live API during
# design. Anchored here so the rest of
# the file reads like a behavior spec rather than ID soup.
POPULATION_ID = "0003443838"   # monthly population estimates
GDP_ID = "0003109741"          # quarterly GDP advance
TRADE_ID = "0004049306"        # foreign trade — 3.8M rows


@pytest.fixture(scope="module")
def app_id() -> str:
    value = os.environ.get("ESTAT_APP_ID")
    if not value:
        pytest.skip("ESTAT_APP_ID not set; skipping live-API integration tests")
    return value


@pytest.fixture(scope="module")
def client(app_id: str) -> EstatClient:
    return EstatClient(app_id=app_id)


class TestBenchmarkTablesStructuredLive:
    """GDP and the population estimates come back structured from ``rule="auto"``
    via the generic Layer A path (no bundled rule) with their time axis
    normalized to the right granularity. (Foreign trade — the one table that
    needs a bundled rule — is excluded by ``max_rows`` and pinned on synthetic
    rows in ``tests/test_builtin_rules.py``.)"""

    def test_population_estimates_normalizes_monthly_time(self, client: EstatClient) -> None:
        # 4,293 rows in production; well under any sane max_rows.
        resp = client.get_stats_data(POPULATION_ID, max_rows=10_000)
        assert len(resp.values) > 0
        # Sample a few rows so transient drift (e.g. one outlier row
        # with a marker value) doesn't fail the assertion.
        sample = resp.values[:5]
        for row in sample:
            time = row["time"]  # canonical time cell
            assert time["granularity"] == "monthly"
            # YYYY-MM
            assert isinstance(time["normalized"], str)
            assert len(time["normalized"]) == 7 and time["normalized"][4] == "-"
            # raw code preserved
            assert "code" in time

    def test_gdp_normalizes_quarterly_time(self, client: EstatClient) -> None:
        # 2,816 rows in production.
        resp = client.get_stats_data(GDP_ID, max_rows=10_000)
        assert len(resp.values) > 0
        sample = resp.values[:5]
        for row in sample:
            time = row["time"]  # canonical time cell
            assert time["granularity"] == "quarterly"
            # YYYY-Qn
            assert "-Q" in time["normalized"]


class TestMaxRowsGuardAgainstHugeTable:
    """The trade table is too big to fetch in CI; the guard exists
    so a curious caller cannot accidentally pull it."""

    def test_too_many_rows_error_is_raised_before_download(self, client: EstatClient) -> None:
        # The probe is a cheap cntGetFlg=Y call, not a data fetch.
        # We pin the count is "huge" (rather than a specific value)
        # so e-Stat publishing new monthly trade rows does not break
        # the test.
        with pytest.raises(TooManyRowsError) as exc:
            client.get_stats_data(TRADE_ID, max_rows=10_000)
        assert exc.value.total > 1_000_000
        assert exc.value.limit == 10_000


class TestRawModeAgainstLiveJson:
    """``rule=None`` must round-trip the raw Layer-2 shape against
    real responses, not just fixtures."""

    def test_raw_mode_keeps_axis_id_keys_and_no_normalization(self, client: EstatClient) -> None:
        resp = client.get_stats_data(GDP_ID, rule=None, max_rows=10_000)
        row = resp.values[0]
        # time is the raw 10-digit code, not the normalized form
        assert row["time"].isdigit()
        assert len(row["time"]) == 10
        # No transformation fields injected
        assert "time_granularity" not in row
        assert "time_code" not in row


class TestProgressCallback:
    """The progress callback must fire at least once on any real
    fetch — silent callbacks are how tqdm progress bars get stuck."""

    def test_progress_event_fires_at_least_once(self, client: EstatClient) -> None:
        events: list[ProgressEvent] = []
        client.get_stats_data(
            GDP_ID,
            max_rows=10_000,
            progress=events.append,
        )
        assert len(events) >= 1
        assert events[0].page == 1
        assert events[0].rows_fetched > 0
        assert events[0].rows_total is not None


class TestListStats:
    """The discovery endpoint must round-trip the DATALIST_INF shape
    against live responses (limit kept small so we do not page through
    every table in the family)."""

    def test_search_by_stats_code_returns_some_tables(self, client: EstatClient) -> None:
        # GDP family is small enough to fit in a single small page.
        resp = client.list_stats(statsCode="00100409", limit=5)
        assert resp.total_number >= 1
        assert len(resp.tables) >= 1
        # TABLE_INF carries @id at minimum
        assert "@id" in resp.tables[0]
