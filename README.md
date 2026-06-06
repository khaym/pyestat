# pyestat

Python client for the [e-Stat API](https://www.e-stat.go.jp/api/) — the official portal for Japanese government statistics — with structured outputs for LLMs and data scientists.

## Status

Phase 0 (Walking Skeleton). The public API is minimal and may change.

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

Register for an `appId` at <https://www.e-stat.go.jp/api/>, then make it
available as the `ESTAT_APP_ID` environment variable (or pass it
explicitly to `EstatClient(app_id=...)`):

```python
from pyestat import EstatClient, EstatApiError

client = EstatClient()  # reads ESTAT_APP_ID from the environment

try:
    response = client.get_stats_data(stats_data_id="0003448237")
except EstatApiError as exc:
    # e-Stat reports logical errors with HTTP 200 + STATUS != 0.
    print(f"e-Stat refused the query: {exc.status} {exc.message}")
else:
    print(response.status)          # 0 on success
    print(response.stats_data_id)   # "0003448237"
    for row in response.values:
        # @-prefixed dimensions become regular keys; "$" becomes "value".
        print(row)
        # -> {"tab": "020", "cat01": "000", "time": "2020000000",
        #     "unit": "千人", "value": "126146"}
```

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

Loading rules from `./rules/*.yaml` is on the roadmap; for now the
`RuleV2` object is constructed directly in Python.

## Configuring the appId

`pyestat` only reads `ESTAT_APP_ID` from `os.environ`. How that variable
gets there is your project's call. A few common patterns:

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
from dotenv import load_dotenv
load_dotenv()
from pyestat import EstatClient
```

**Docker / Compose**: pass `-e ESTAT_APP_ID=...` or set it under
`environment:` in your compose file.

**CI (GitHub Actions, etc.)**: store the appId as an encrypted secret and
inject it as an env var in the workflow step.

**Production**: pull it from your secret manager
(AWS Secrets Manager / GCP Secret Manager / HashiCorp Vault / ...) at
startup and set `os.environ["ESTAT_APP_ID"]` before constructing
`EstatClient`.

`pyestat` deliberately avoids bundling a dotenv loader so it does not
constrain how you manage secrets.

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
