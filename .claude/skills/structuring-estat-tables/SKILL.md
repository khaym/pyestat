---
name: structuring-estat-tables
description: >-
  Guides a user from an e-Stat statsDataId and an intent to the data that
  actually serves it, surfacing e-Stat's structural pitfalls (a time axis
  mixing calendar and fiscal years, aggregate rows double-counting detail, an
  unexpected value spread) and, when the table's structure is not covered,
  authoring a durable pyestat conversion rule. Use when a user wants to fetch,
  structure, or make sense of an e-Stat table with pyestat, when a table is not
  covered by a built-in rule, or when they want to write a conversion rule.
  Triggers include "e-Stat の表を使いたい / 構造化したい", "この statsDataId をどう取れば
  いい", "未対応の表を使いたい", "変換ルールを作りたい", "この表の中身を確認したい".
---

# Structuring e-Stat tables

Take a user from **an e-Stat `statsDataId` + what they want** to **the data
that serves it** — and, only when the table's structure is not covered, to a
durable conversion rule they can keep.

The hard part of e-Stat is rarely "no rule exists." It is that a table's real
content surprises the user: a time axis silently mixes calendar years (`2015年`)
and fiscal years (`2015年度`); aggregate rows sit intermixed with detail and
double-count; a value type is spread across rows. A conversion rule alone does
not fix these — the user must first *see* what is in the table and *choose* the
slice they mean. This skill makes that visible.

**Posture — run the code for the user.** Assume the user may not be a developer.
You run the pyestat calls, read the metadata and a sample, profile the data,
draft any YAML, and report findings in plain language for their approval. Hand a
developer the snippets if they prefer, but never require them to write Python or
YAML themselves.

**Do not hardcode pitfalls.** e-Stat's pitfalls are open-ended. Inspect the
actual members and a data sample and reason about what would surprise *this*
user's intent — the examples below are illustrations, not a fixed checklist.

## Contents

- [Setup](#setup) — the appId and client
- [Flow](#flow) — 1 capture intent → 2 read the table → 3 diagnose pitfalls →
  4 branch (narrow or author) → 5 author a rule → 6 confirm
- [Error handling](#error-handling)

## Setup

The user needs a valid e-Stat appId in `ESTAT_APP_ID` (see the pyestat README
"Configuring the appId"). Build the client once:

```python
import os
from pyestat import EstatClient

client = EstatClient(app_id=os.environ["ESTAT_APP_ID"])
```

Run snippets in the user's project environment (whatever runs their pyestat —
e.g. `python`, `uv run python`). If `ESTAT_APP_ID` is unset, ask the user to set
it before continuing; do not invent one.

## Flow

### 1. Capture intent and the table

Confirm two things in the user's own words: **what they want** (a metric, a
region, a time span, a granularity) and **the `statsDataId`**. If they do not
have an id, point them at e-Stat's search or `client.list_stats(...)` to find
one — table discovery is otherwise out of scope for this skill.

### 2. Read how pyestat sees the table

```python
exp = client.explain_table(stats_data_id)
exp.role_pattern    # e.g. ('meta-axis', 'category', 'area', 'time')
exp.coverage        # 'builtin' | 'user' | 'project' | 'generic' | 'fallback'
for a in exp.axes:
    print(a.axis_id, a.name, a.role, a.confidence)
exp.proposed_rule   # a RuleV2 starting point, or None
```

Explain in plain language what each axis *is* (time / area / category / a
value-spread `meta-axis`) and what `coverage` means for them:

- `builtin` / `user` / `project` — a specific rule already structures this table.
- `generic` — no specific rule, but the auto path structures it from the
  axis roles; `proposed_rule` shows what it produces.
- `fallback` — the table is too ambiguous or not flatly structurable; the
  auto path preserves raw rows (lossless), and a hand-written rule is what
  turns it into clean columns.

### 3. Diagnose the pitfalls (the core of this skill)

Read the raw members and a small sample, then flag anything that would derail
the user's intent. Most checks need only metadata:

```python
meta = client.get_meta_info(stats_data_id)
for axis in meta.class_objs:
    print(axis.id, axis.name, len(axis.classes))
    # inspect axis.classes: each member has code / name / level / parentCode
```

What to look for (examples, not a fixed list) — reason from the members:

- **Time granularity mix.** e-Stat encodes calendar year as `YYYY000000`
  (`2015年`), fiscal year as `YYYY100000` (`2015年度`), monthly as `YYYY00MMMM`.
  If one `time` axis carries more than one of these — and especially if calendar
  and fiscal sit at the same `level` — tell the user: filtering by `level` alone
  will not separate them; they must choose which series they mean.
- **Aggregate vs. detail.** Members with a `parentCode` form a hierarchy; totals
  and their components coexist and will double-count if summed. pyestat's
  `aggregates="exclude"` / `"only"` selects one grain — confirm which the user
  wants.
- **Unexpected value spread.** A `meta-axis` means several measures (quantity /
  amount / unit …) are split across rows; the user gets one row per measure
  unless the table is pivoted.

To see the actual values, fetch a **small** sample — narrow with `select` so the
fetch stays tiny — and profile it:

```python
import pandas as pd

resp = client.get_stats_data(
    stats_data_id,
    rule="auto",
    select={"area": "00000"},   # narrow to keep the sample small
)
df = pd.DataFrame(resp.to_flat())
df["time_granularity"].value_counts()   # spot a granularity mix in the data
df["time"].head(20)                      # e.g. "2015" vs "2015-04"
```

Report what you found plainly ("this annual slice contains both calendar-year
and fiscal-year figures — which do you want?") before doing anything else.

### 4. Branch: narrow, or author

Whether to author a rule is a judgment against the user's intent — **not** a
lookup on `coverage`. A covered table (`builtin` / `generic` / …) still fails
the intent when its columns cannot express the distinction the user needs. So
fetch a sample of the auto output and read its columns against the intent from
step 1: does it carry every distinction they want, at their grain, with each
measure in its own column?

```python
resp = client.get_stats_data(stats_data_id, rule="auto")
pd.DataFrame(resp.to_flat()).head()   # weigh these columns against the intent
```

**If the auto output serves the intent** — the common case — narrow it with
`select` (server-side), `aggregates=`, and any client-side filter, then deliver
the data. No rule is needed.

```python
resp = client.get_stats_data(
    stats_data_id,
    rule="auto",
    select={"cat01": "0001", "area": "00000", "time": {"level": "1"}},
)
df = pd.DataFrame(resp.to_flat())
df = df[df["time"].str.fullmatch(r"\d{4}")]   # e.g. keep calendar years only
```

**If it does not** — go to step 5 and author a rule. The reasons an auto output
fails an intent are open-ended; judge from the sample, not a fixed list. Some
recurring shapes:

- **Raw rows, no clean columns** (`coverage` is `fallback`): the table was too
  ambiguous to flatten automatically.
- **Two same-role axes collapsed into one column** — 常住地 × 従業地, 建築主 ×
  用途. The role pattern matches (coverage may even be `builtin` / `generic`),
  but one role→column mapping cannot keep them apart; each needs its own column
  via `source.axis`.
- **A measure spread down rows** — a `meta-axis` the auto path left unpivoted,
  so the user gets one row per measure instead of one column each.
- **Unhelpful names** — the columns are complete and correct but named or
  labeled so the intended slice is hard to read.

### 5. Author a durable rule (only when needed)

Start from `exp.proposed_rule` when it is present and refine it *with* the user;
when it is `None` (a `fallback` table, or one no generic rule fits), build the
rule from the `role_pattern` and axis roles `explain_table` reported. What you
refine follows from why the auto output missed (step 4):

- **Two same-role axes collapsed** → give each its own column with `source.axis`.
- **A measure spread down rows** → pivot the `meta-axis` with `where` / `key`.
- **Unhelpful names** → set the column names and label maps.

The RuleV2 schema (short vs long form, splitting same-role axes, pivoting a
`meta-axis`, naming columns for `to_flat()`) is documented in
**`docs/AUTHORING_RULES.md`** — follow it rather than restating it here.

Save the rule where pyestat auto-discovers it — the project layer — so it
applies with no code change and can be committed:

```python
from pathlib import Path
Path("pyestat_rules").mkdir(exist_ok=True)
Path("pyestat_rules/<descriptive-name>.yaml").write_text(rule_yaml, encoding="utf-8")
```

Then **verify** by re-running and checking the columns match the user's intent:

```python
resp = EstatClient(app_id=os.environ["ESTAT_APP_ID"]).get_stats_data(
    stats_data_id, rule="auto",
)
pd.DataFrame(resp.to_flat()).head()
```

A malformed rule raises a typed `RuleLoadError` at client construction — fix the
YAML the message names and retry.

### 6. Confirm

Show the final data (or the saved rule + a sample of its output) and confirm it
matches the intent from step 1. If a rule was saved, tell the user it lives in
`./pyestat_rules/`, applies automatically, and can be shared via git.

## Error handling

- **Transient e-Stat error** ("データベースにアクセス中にエラー…時間をおいて"): retry after
  a short wait; do not treat it as a code failure.
- **`TooManyRowsError`**: the fetch is too large — narrow with `select`, or pass
  `max_rows=` when the user accepts a partial fetch.
- **`RuleLoadError`**: a saved/edited rule file is malformed; fix the file the
  error names.
- **Missing `ESTAT_APP_ID`**: ask the user to set it; never fabricate one.
- **Goal integrity**: if a fetch fails, say so — never present fabricated or
  stale data as if the table were structured successfully.
