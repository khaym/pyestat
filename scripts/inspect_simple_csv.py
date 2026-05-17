"""Fetch getSimpleStatsData (CSV) for a stats_data_id and print the response head/tail."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

STATS_DATA_ID = sys.argv[1] if len(sys.argv) > 1 else "0003443838"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 30  # cap rows when probing huge tables
APP_ID = os.environ["ESTAT_APP_ID"]

# R library uses rest/3.0/app/getSimpleStatsData (no "json" segment).
# sectionHeaderFlg: 1 = include headers (default), 2 = data only.
res = httpx.get(
    "https://api.e-stat.go.jp/rest/3.0/app/getSimpleStatsData",
    params={
        "appId": APP_ID,
        "statsDataId": STATS_DATA_ID,
        "limit": LIMIT,
        "lang": "J",
    },
    timeout=60.0,
)
res.raise_for_status()
text = res.text

print(f"Content-Type: {res.headers.get('content-type')}")
print(f"Response bytes: {len(text):,}")
print(f"Response lines: {text.count(chr(10)):,}")
print("=" * 70)
print(text)
