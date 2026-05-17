"""Integration tests that hit the live e-Stat API.

These run only when ESTAT_APP_ID is set in the environment. The default
target table is "人口推計 2020年" (statsDataId 0003448237); override with
ESTAT_TEST_STATS_DATA_ID if that ID is invalid in your account or you
want to point at a different table.
"""
from __future__ import annotations

import os

import pytest

from pyestat import EstatClient


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("ESTAT_APP_ID"),
        reason="ESTAT_APP_ID is not set; skipping live e-Stat API integration tests.",
    ),
]


def test_get_stats_data_against_live_api() -> None:
    stats_data_id = os.environ.get("ESTAT_TEST_STATS_DATA_ID", "0003448237")

    client = EstatClient()
    response = client.get_stats_data(stats_data_id=stats_data_id)

    assert response.status == 0
    assert response.stats_data_id == stats_data_id
    assert len(response.values) > 0
    assert "value" in response.values[0]
