# pyestat

Python client for the [e-Stat API](https://www.e-stat.go.jp/api/) — the official portal for Japanese government statistics — with structured outputs for LLMs and data scientists.

## Status

pyestat is pre-1.0. Two parts of the surface move at different speeds:

- **Settled** — what you *consume*: the nested `StatsDataResponse` shape
  (with its `to_flat()` projection) and the `EstatError` hierarchy hold
  across 0.x.
- **Evolving** — what you *author*: the `RuleV2` rule schema may still
  change across 0.x as built-in coverage grows.

## Why another e-Stat library?

The e-Stat API ships JSON that is a thin re-encoding of the original XML schema:
dimension codes live under `@`-prefixed keys, cell values live under `$`,
and logical errors are reported with HTTP 200 plus a non-zero `RESULT.STATUS`.
Existing Python wrappers stop at "give me a DataFrame" and pass these quirks
through to the caller. `pyestat` flattens the encoding and surfaces typed
results so an LLM agent or a researcher can consume responses without
learning the e-Stat wire format.

## Install

`pyestat` is not yet published to PyPI. For now, install from a local checkout:

```sh
uv add /path/to/pyestat
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

## Supplying your own rules

`pyestat` ships built-in transformation rules for a small set of
tables; pass `user_rules=` to override them or add coverage for a
table that has none. A rule declares the **output columns** you want,
each drawn from an axis *role* the classifier infers — so one rule
covers every table sharing that role pattern. A user rule matching a
table's role pattern shadows a built-in for the same pattern:

```python
from pyestat import EstatClient, RuleV2

custom = RuleV2.model_validate({
    "schema_version": "2",
    "match": {"role_pattern": ["value", "area", "time"]},
    "output": [
        {"column": "year",   "source": {"role": "time"},  "transform": "yearly"},
        {"column": "region", "source": {"role": "area"},  "transform": "passthrough"},
        {"column": "value",  "source": {"role": "value"}, "transform": "passthrough"},
    ],
})

client = EstatClient(user_rules=[custom])
```

### Folding spread rows into one record (pivot)

Some tables split one logical record across several rows — a `meta-axis`
the classifier flags, such as foreign trade's quantity / amount / unit. A
`where` predicate on a `meta-axis` source folds those rows by the remaining
axes and selects each measure into its own column, matching on the member
**name** (not its opaque code):

```python
trade = RuleV2.model_validate({
    "schema_version": "2",
    "match": {"role_pattern": ["category", "meta-axis", "area", "time"]},
    "output": [
        {"column": "cat01", "source": {"role": "category"}},
        {"column": "area",  "source": {"role": "area"}},
        {"column": "time",  "source": {"role": "time"}, "transform": "yearly"},
        {"column": "amount_jpy", "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}}},
        {"column": "quantity",   "source": {"role": "meta-axis", "where": {"equals": "合計_数量2"}}},
        {"column": "unit",       "source": {"role": "meta-axis", "where": {"equals": "単位2"}}},
    ],
})
```

A measure absent from a group (e.g. a series retired in a base-year
revision) yields `None` for that column rather than dropping the record, so
the output shape stays stable across table versions. Conversely, a `where`
predicate is a projection: meta-axis members you do not select (e.g. a
table's monthly breakdowns when you keep only the annual totals) are dropped
from the output — declare a column for every member you need.

You can also drop rule files in a directory instead of building them in
Python: `EstatClient` discovers `./pyestat_rules/*.yaml` (and `.yml`) by
file placement alone — no registration call. Each file is the same schema
as the `RuleV2` above, written as YAML.

- Relocate the directory with `project_rules_dir=`, or opt out with
  `None` / `""`.
- An invalid rule file raises `RuleLoadError` at construction, so a typo
  surfaces immediately rather than at query time.

## Error behavior

On the default `rule="auto"` path, whether a *rule* failure reaches you
turns on who authored the failing rule — fall back when it is pyestat's,
surface when it is yours:

- A **built-in** rule that cannot apply degrades to lossless raw output
  instead of raising: its failure is internal and you cannot edit it, so
  preserved data beats a crash.
- A rule **you** supplied — an explicit `rule=RuleV2(...)`, a
  `user_rules=` entry, or a file in `./pyestat_rules` — that cannot apply
  raises a typed error so you can fix it and re-run.

So `get_stats_data(id)` on a table pyestat does not yet handle returns
usable raw rows rather than failing, while a mistake in your own rule is
reported.

Every pyestat error inherits from `EstatError`, so a coarse
`except EstatError` catches them all; catch a leaf (`EstatApiError`,
`RuleLoadError`, …) when you want to act on one case.

## Configuring the appId

`pyestat` takes the appId explicitly via `EstatClient(app_id=...)` and never
reads the environment itself. A common convention is to keep it in an
`ESTAT_APP_ID` environment variable and pass `os.environ["ESTAT_APP_ID"]`;
how that variable gets there is your project's call. A few common patterns:

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

MIT License. See [LICENSE](LICENSE) for details.
