"""One-off inspector: dump raw e-Stat JSON and pyestat's parsed view side by side."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from pyestat import EstatClient  # noqa: E402

STATS_DATA_ID = sys.argv[1] if len(sys.argv) > 1 else "0003443838"
APP_ID = os.environ["ESTAT_APP_ID"]

# 1) Raw API response
raw = httpx.get(
    "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData",
    params={"appId": APP_ID, "statsDataId": STATS_DATA_ID},
    timeout=30.0,
).json()

# Truncate VALUE array for readability but keep the rest of the structure intact.
payload = raw["GET_STATS_DATA"]
values = payload["STATISTICAL_DATA"]["DATA_INF"]["VALUE"]
if isinstance(values, list):
    payload["STATISTICAL_DATA"]["DATA_INF"]["VALUE"] = values[:3] + [f"... ({len(values)} total)"]

print("=" * 70)
print("RAW e-Stat JSON (VALUE truncated to first 3 rows)")
print("=" * 70)
print(json.dumps(raw, ensure_ascii=False, indent=2))

# 2) Library's parsed view
print()
print("=" * 70)
print("pyestat EstatClient.get_stats_data() result")
print("=" * 70)
client = EstatClient()
response = client.get_stats_data(stats_data_id=STATS_DATA_ID)
print(f"status:        {response.status}")
print(f"error_msg:     {response.error_msg!r}")
print(f"stats_data_id: {response.stats_data_id}")
print(f"values:        list of {len(response.values)} dict (first 3 shown)")
for row in response.values[:3]:
    print(f"  {row}")
