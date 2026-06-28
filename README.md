<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/khaym/pyestat/main/assets/logo-dark.png">
    <img src="https://raw.githubusercontent.com/khaym/pyestat/main/assets/logo.png" alt="pyestat" width="420">
  </picture>
</p>

<p align="center">
  <em>Structured, typed results from Japan's official statistics portal
  (<a href="https://www.e-stat.go.jp/api/">e-Stat</a>) — ready for LLMs and data scientists.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/pyestat/"><img src="https://img.shields.io/pypi/v/pyestat?color=082060" alt="PyPI version"></a>
  <a href="https://pypi.org/project/pyestat/"><img src="https://img.shields.io/pypi/pyversions/pyestat?color=082060" alt="Python versions"></a>
  <a href="https://github.com/khaym/pyestat/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT"></a>
</p>

<p align="center">
  <a href="#why-another-e-stat-library">Why</a> &bull;
  <a href="#install">Install</a> &bull;
  <a href="#usage">Usage</a> &bull;
  <a href="https://github.com/khaym/pyestat/blob/main/docs/AUTHORING_RULES.md">Writing rules</a> &bull;
  <a href="#license">License</a>
</p>

<p align="center">
  <b>English</b> &bull; <a href="https://github.com/khaym/pyestat/blob/main/README.ja.md">日本語</a>
</p>

## Why another e-Stat library?

The e-Stat API returns JSON that is a thin re-encoding of the original XML:
dimension codes hide under `@`-prefixed keys and the cell value under `$`, while
the human-readable labels and units for those codes live in a separate
`CLASS_INF` block you must join yourself. Logical errors arrive as HTTP 200 with
a non-zero `RESULT.STATUS`. Existing Python wrappers stop at "give me a
DataFrame" and pass these quirks through to the caller.

`pyestat` resolves them. By default (`rule="auto"`) it classifies each axis and
returns self-describing cells — no per-table rule required:

```python
# e-Stat's raw VALUE cell: codes only — labels and units live in CLASS_INF
{"@cat01": "000", "@time": "2020000000", "$": "126146"}

# pyestat (rule="auto"): codes resolved to labels, value carries its unit
{"cat01": {"code": "000", "label": "男女計"},
 "time":  {"code": "2020000000", "label": "2020年",
           "normalized": "2020", "granularity": "yearly"},
 "value": {"value": "126146", "unit": "千人"}}
```

So an LLM agent or a researcher consumes a response without learning the e-Stat
wire format, joining `CLASS_INF` by hand, or special-casing the HTTP-200 error
channel.

### What you get

**Find and fetch**

- Search the catalog with `list_stats` (`searchWord`, `statsCode`, …).
- Inspect a table's axes with `get_meta_info` before downloading anything.
- Stream millions of rows page by page with `iter_stats_data_pages` — cap a
  fetch up front with `max_rows` (raises `TooManyRowsError`) or follow it with a
  `progress` callback.

**Structure** (`rule="auto"`, the default — no rule needed)

- Dimension codes resolved to `{code, label}` — `CLASS_INF` joined for you.
- Time normalized, with granularity tagged (yearly / monthly / …).
- A measure spread across rows folded into one record, key auto-detected — for
  tables whose measure axis is flat (GDP, CPI, 建築着工 …). Hierarchical crosses
  (trade's measure × period) and multi-category tables stay as lossless raw.
- `aggregates="exclude"` drops subtotal/total rows for a sum-safe leaf grain
  (`"only"` keeps just the totals), read from `@parentCode` — a fetch option, so
  it works in any mode.
- Your own `RuleV2` adds domain-specific column names and explicit pivots of
  hierarchical crosses (`where` / `key` / `unit_from`).

**Hand off**

- `to_flat()` projects the nested cells to one column per field, for pandas.

Throughout, values pass through verbatim — numbers stay strings and suppression
markers (`-` / `***` / `X`) are preserved — e-Stat's HTTP-200 logical errors
surface as a typed `EstatApiError`, and transient network failures are retried.

## Status

pyestat is pre-1.0. Two parts of the surface move at different speeds:

- **Settled** — what you *consume*: the nested `StatsDataResponse` shape
  (with its `to_flat()` projection) and the `EstatError` hierarchy hold
  across 0.x.
- **Evolving** — what you *author*: the `RuleV2` rule schema may still
  change across 0.x as built-in coverage grows.

## Install

```sh
uv add pyestat
# or
pip install pyestat
```

## Usage

Register for an `appId` at <https://www.e-stat.go.jp/api/>, then pass it
explicitly to `EstatClient(app_id=...)`. A common convention is to keep it
in an `ESTAT_APP_ID` environment variable and read it yourself:

```python
import os

from pyestat import EstatClient, EstatApiError

client = EstatClient(app_id=os.environ["ESTAT_APP_ID"])

try:
    response = client.get_stats_data(stats_data_id="0003448237")
except EstatApiError as exc:
    # e-Stat reports logical errors with HTTP 200 + STATUS != 0.
    print(f"e-Stat refused the query: {exc.status} {exc.message}")
else:
    print(response.stats_data_id)   # "0003448237"
    for row in response.values:
        # The default rule="auto" returns self-describing *nested* cells:
        # each axis is {code, label}, time adds normalized/granularity,
        # the observation is {value, unit}.
        print(row)
        # -> {"cat01": {"code": "000", "label": "男女計"},
        #     "time":  {"code": "2020000000", "label": "2020年",
        #               "normalized": "2020", "granularity": "yearly"},
        #     "value": {"value": "126146", "unit": "千人"}}
```

Prefer one column per field (e.g. for pandas)? `to_flat()` projects the
nested cells to the familiar suffix shape — losslessly, and as a no-op on a
raw (`rule=None`) response:

```python
flat = response.to_flat()
# -> [{"cat01": "000", "cat01_label": "男女計",
#      "time": "2020", "time_code": "2020000000",
#      "time_label": "2020年", "time_granularity": "yearly",
#      "value": "126146", "unit": "千人"}, ...]

import pandas as pd
df = pd.DataFrame(flat)
```

Pass `rule=None` instead to get e-Stat's raw rows unchanged (`@`-prefixed
dimensions become plain keys, `"$"` becomes `"value"`) — flat scalars, no
labels or normalization.

## Writing your own rules

`pyestat` ships built-in rules for a small set of tables and falls back to
`rule="auto"` for the rest. When you want different structuring — or
domain-specific column names — supply your own `RuleV2`:

```python
from pyestat import EstatClient, RuleV2

custom = RuleV2.model_validate({
    "schema_version": "2",
    "match": {"role_pattern": ["value", "area", "time"]},
    "output": [
        {"column": "year",   "source": {"role": "time"}, "transform": "yearly"},
        {"column": "region", "source": {"role": "area"}},
        {"column": "value",  "source": {"role": "value"}},
    ],
})

client = EstatClient(user_rules=[custom])
```

A rule declares the **output columns** you want, each drawn from an axis *role*
the classifier infers, so one rule covers every table sharing that role pattern.
Pivoting rows split across a `meta-axis`, naming columns for `to_flat()`, and
dropping rule files into a directory are covered in
**[Writing rules →](https://github.com/khaym/pyestat/blob/main/docs/AUTHORING_RULES.md)**.

> The `RuleV2` schema is evolving across 0.x — see [Status](#status).

## Error behavior

On the default `rule="auto"` path, whether a *rule* failure reaches you
turns on who authored the failing rule — fall back when it is pyestat's,
surface when it is yours:

- A built-in rule that cannot apply degrades to lossless raw output
  instead of raising: its failure is internal and you cannot edit it, so
  preserved data beats a crash.
- A rule you supplied — an explicit `rule=RuleV2(...)`, a
  `user_rules=` entry, or a file in `./pyestat_rules` — that cannot apply
  raises a typed error so you can fix it and re-run.

So `get_stats_data(id)` on a table pyestat does not yet handle returns
usable raw rows rather than failing, while a mistake in your own rule is
reported.

Every pyestat error inherits from `EstatError`, so a coarse
`except EstatError` catches them all; catch a leaf (`EstatApiError`,
`TooManyRowsError`, …) when you want to act on one case.

## Configuring the appId

[Usage](#usage) shows the basic convention — pass `app_id` explicitly, kept in
an `ESTAT_APP_ID` variable. How that variable reaches the environment is your
project's call; a few common patterns:

**Shell export** (interactive use):

```sh
export ESTAT_APP_ID="<your-app-id>"
python your_script.py
```

**`.env` file + [python-dotenv](https://github.com/theskumar/python-dotenv)**
(local development, Jupyter):

```sh
echo 'ESTAT_APP_ID=<your-app-id>' > .env
# in your code (or notebook cell):
```

```python
import os

from dotenv import load_dotenv
from pyestat import EstatClient

load_dotenv()
client = EstatClient(app_id=os.environ["ESTAT_APP_ID"])
```

**Docker / Compose**: pass `-e ESTAT_APP_ID=...` or set it under
`environment:` in your compose file.

**CI (GitHub Actions, etc.)**: store the appId as an encrypted secret and
inject it as an env var in the workflow step.

**Production**: pull it from your secret manager
(AWS Secrets Manager / GCP Secret Manager / HashiCorp Vault / ...) at
startup and pass it to `EstatClient(app_id=...)`.

`pyestat` deliberately avoids reading the environment or bundling a dotenv
loader, so it does not constrain how you manage secrets.

## Development

```sh
uv sync                              # install runtime + dev deps
cp .env.example .env                 # then fill in your ESTAT_APP_ID
uv run pytest                        # runs unit + live API tests
uv run pytest -m "not integration"   # unit only (no network)
```

The live integration test under `tests/test_get_stats_data_integration.py`
auto-skips if `ESTAT_APP_ID` is not set, so the unit suite stays
hermetic without extra flags.

## License

MIT License. See [LICENSE](https://github.com/khaym/pyestat/blob/main/LICENSE) for details.
