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

`pyestat` resolves them. With no per-table setup, each code comes back joined to
its label and each value to its unit (the default, `rule="auto"`):

```python
# e-Stat's raw VALUE cell: codes only — labels and units live in CLASS_INF
{"@cat01": "000", "@time": "2020000000", "$": "126146"}

# pyestat (rule="auto"): codes resolved to labels, value carries its unit
{"cat01": {"code": "000", "label": "男女計"},
 "time":  {"code": "2020000000", "label": "2020年",
           "normalized": "2020", "granularity": "yearly"},
 "value": {"value": "126146", "unit": "千人"}}
```

And where e-Stat splits one observation across rows — one row per measure — it
folds them back into a single record, one column per measure:

```python
# e-Stat: each measure on its own row, sharing the same time
{"@time": "2020000000", "@tab": "001", "$": "16"}                     # 数量
{"@time": "2020000000", "@tab": "002", "$": "35220", "@unit": "千円"}  # 金額

# pyestat (rule="auto"): one record, the measures pivoted into columns
{"time": {"code": "2020000000", "label": "2020年",
          "normalized": "2020", "granularity": "yearly"},
 "数量": {"value": "16",    "unit": None},
 "金額": {"value": "35220", "unit": "千円"}}
```

So an LLM agent or a researcher consumes a response without learning the e-Stat
wire format, joining `CLASS_INF` by hand, reshaping rows, or special-casing the
HTTP-200 error channel.

### What you get

**Get e-Stat's scattered data into a shape you can analyze**

*The default `rule="auto"` — no rule to write. Codes come back resolved to their
labels and values to their units; on top of that:*

- One observation split across rows, folded back into one row — e-Stat returns
  each measure on its own row; pyestat detects which columns are keys and which
  are measures to fold, and returns one record with a column per measure.
- Mixed time formats lined up so you can compare them — e-Stat returns years,
  months, and quarters in a different format per table, as opaque codes
  (`2020000000`, `1994000103`). pyestat normalizes them to `2020` / `2020-03`
  / `1994-Q1` and tags the granularity (yearly / monthly / quarterly), so you can
  sort and join across granularities and across tables.

  ```python
  # e-Stat: the quarter arrives as an opaque time code (meaning lives in CLASS_INF)
  {"@time": "1994000103", ...}          # CLASS_INF label: "1994年1～3月期"

  # pyestat: normalized so you can sort and align it, with the granularity tagged
  {"time": {"code": "1994000103", "label": "1994年1～3月期",
            "normalized": "1994-Q1", "granularity": "quarterly"}, ...}
  ```

- Keep only the figures you can sum — `aggregates="exclude"` drops the
  subtotal and total rows, `"only"` keeps just the totals, so you never
  double-count by summing a subtotal back in.

**Pull just the slice you need from a huge table**

- Millions of rows narrowed to the few hundred you want — `select` filters
  server-side by item / region / period, so the consumer price index (~13M rows
  unfiltered) returns only the rows you ask for, keyed by the same axis ids
  `get_meta_info` shows.
- A large table without loading it all into memory — `iter_stats_data_pages`
  yields one page at a time. Cap a runaway pull with `max_rows` (raises
  `TooManyRowsError`), and track a long one with a `progress` callback.

**Find the table you need**

- Locate the table you want in the catalog by keyword or code — `list_stats`
  takes e-Stat's own getStatsList parameters (`searchWord`, `statsCode`, …; see
  the [e-Stat API manual](https://www.e-stat.go.jp/api/api-info/e-stat-manual3-0)
  for their meaning). An unknown name is rejected up front, not sent silently —
  which would return the entire catalog.
- Check a table's axes (the codes you filter on) before you fetch it —
  `get_meta_info`.

**Hand off to your tools**

- One column per field for pandas and friends — `to_flat()`.

Hit a table pyestat doesn't fold yet, or want column names of your own? That is a
short rule away — see [Writing your own rules](#writing-your-own-rules).

Throughout, the numbers are never rewritten: they stay strings, and e-Stat's
suppression markers (`-` / `***` / `X`) are preserved as-is. e-Stat's habit of
reporting errors as HTTP 200 surfaces as a typed `EstatApiError`, and dropped
connections are retried for you.

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

### Fetching one slice of a large table

Some tables are too big to pull whole — the consumer price index is millions of
rows across every item, region, and period. `select` filters server-side, keyed
by the axis ids `get_meta_info` reports, so you fetch only the slice you want:

```python
# 総合 (all items) × 全国 (nationwide) × 指数 (the index), annual rows only
resp = client.get_stats_data(
    "0003427113",  # 2020-base CPI — ~13M rows unfiltered
    select={"cat01": "0001", "area": "00000", "tab": "1", "time": {"level": "1"}},
)
# a few hundred rows, structured exactly as above — not the whole catalog
```

A `select` value is a code, a list of codes, or a mapping setting any of
`code` / `level` (a single level or a range) / `from` / `to` (an inclusive
code range) — each key optional, as the example's `time: {"level": "1"}`
shows. The codes are e-Stat's own — pyestat passes them through as-is, with no
catalog lookup, so a wrong code isn't flagged client-side; e-Stat just returns
no rows for it. Read the codes off `get_meta_info`:

```python
for axis in client.get_meta_info("0003427113").class_objs:
    print(axis.id, axis.name, len(axis.classes))  # axis id, name, member count
```

The result is an ordinary response — `to_flat()` it into `pandas` and take
your analysis from there.

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
Not sure of a table's role pattern? `client.explain_table(id)` reports how
pyestat reads it — the `role_pattern` your `match` must equal, each axis's role
and confidence, which layer would handle it (a specific rule, the generic auto
path, or the lossless fallback), and a proposed rule to hand-edit. It also hands
back the table metadata it fetched (on `exp.meta`), so authoring reads a
member's code from the same result without a separate `get_meta_info`. Pivoting rows split across a `meta-axis`, naming columns for
`to_flat()`, and dropping rule files into a directory are covered in
**[Writing rules →](https://github.com/khaym/pyestat/blob/main/docs/AUTHORING_RULES.md)**.

> The `RuleV2` schema is evolving across 0.x — see [Status](#status).

### Guided structuring with Claude Code

If you use [Claude Code](https://claude.com/claude-code), this repo includes a
skill, `structuring-estat-tables` (in `.claude/skills/`, not the PyPI package),
that runs the whole flow for you conversationally — no Python or YAML required.
It reads a table's axes and a data sample, flags e-Stat's structural traps (a
time axis mixing calendar and fiscal years, aggregate rows that double-count
detail), helps you pick the slice you actually mean, and drafts a conversion
rule when no built-in rule covers the table. See [the skill](https://github.com/khaym/pyestat/blob/main/.claude/skills/structuring-estat-tables/SKILL.md).

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
