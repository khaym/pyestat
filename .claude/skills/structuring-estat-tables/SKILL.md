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
- [Flow](#flow) — 1 capture intent → 2 read the axes & codes → 3 diagnose
  pitfalls → 4 branch (narrow or author) → 5 author a rule → 6 confirm
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
one. Its parameters are e-Stat's own search conditions (`searchWord`,
`statsCode`, `surveyYears`, `collectArea` …); read the e-Stat API manual
(getStatsList) for what each means — pyestat forwards them by their published
names and rejects an unknown name rather than guessing. Table discovery is
otherwise out of scope for this skill.

### 2. Read the table's axes and codes

```python
meta = client.get_meta_info(stats_data_id)
for axis in meta.class_objs:
    print(axis.id, axis.name, len(axis.classes))
    # each member is a dict — index by key, not attribute:
    #   axis.classes[0]["code"], ["name"], .get("level")
    #   (some tables also carry parentCode / unit)
```

Explain in plain language what each axis *is* (time / area / category / a
value-spread `meta-axis`), then note that **these are the words `select` uses.**
`select` is keyed by the axis `id` you just printed (`axis.id` — `cat01`,
`area`, `time` …) and valued by a member's `code` — a single code, a list of
codes, or a `{code, level, from, to}` spec (`code` is a dict key, e.g.
`axis.classes[0]["code"]`). So reading the members here is also how you learn
what to pass to `select` in steps 3–4 — pyestat exposes the same ids in
`get_meta_info`, the parsed rows, and `select`, with no wire-only `cd` / `lv`
prefix. (Deciding *how* pyestat structures the table — its role pattern and
whether a rule already covers it — is deferred to step 5, `explain_table`, and is
only needed if the auto output falls short.)

### 3. Diagnose the pitfalls (the core of this skill)

From the same members (step 2) and a small sample, flag anything that would
derail the user's intent. Most checks need only the metadata already in hand.

What to look for (examples, not a fixed list) — reason from the members:

- **Time axis mix.** e-Stat encodes calendar year as `YYYY000000` (`2015年`),
  fiscal year as `YYYY100000` (`2015年度`), monthly as `YYYY00MMMM`. Two distinct
  mixes hide here: a **granularity** mix (yearly vs monthly) and a **year-basis**
  mix (暦年 vs 年度). The latter is *not* a granularity difference — both parse
  as `yearly` (calendar → `2015`, fiscal → `2015-04`) — so it needs a different
  check (see the snippet below). If one `time` axis carries more than one —
  especially calendar and fiscal at the same `level` — filtering by `level`
  alone will not separate them; the user must choose which series they mean.
- **Aggregate vs. detail.** Members with a `parentCode` form a hierarchy; totals
  and their components coexist and will double-count if summed. pyestat's
  `aggregates="exclude"` / `"only"` selects one grain — confirm which the user
  wants.
- **Unexpected value spread.** A `meta-axis` holds several measures (quantity /
  amount / unit …) spread across rows. The generic auto path **pivots them into
  one column per measure** for you; the user gets one row per measure only when
  the table rides the fallback (auto cannot pivot it) — that is when a hand rule
  is needed (step 5).

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
df["time_granularity"].value_counts()   # granularity mix only: yearly vs monthly
# calendar vs fiscal is NOT a granularity difference — both are "yearly".
# separate them by the normalized value / raw code / label instead:
df["time"].head(20)          # "2015" (暦年) vs "2015-04" (年度)
df["time_code"].head(20)     # ...000000 (暦年) vs ...100000 (年度)
df["time_label"].head(20)    # "2015年" vs "2015年度"
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

- **Raw rows, no clean columns**: the table was too ambiguous to flatten
  automatically (it rode the lossless fallback; step 5's `coverage` confirms).
- **Two same-role axes collapsed into one column** — 常住地 × 従業地, 建築主 ×
  用途. The role pattern matches (coverage may even be `builtin` / `generic`),
  but one role→column mapping cannot keep them apart; each needs its own column
  via `source.axis`.
- **A measure spread down rows** — a `meta-axis` the auto path left unpivoted,
  so the user gets one row per measure instead of one column each.
- **Unhelpful column names** — the columns are complete and correct but their
  *names* are hard to read; a rule can rename them. (Unhelpful *value* labels are
  e-Stat's own — a rule cannot remap them; see step 5.)

### 5. Author a durable rule (only when needed)

Now — and only now — call `explain_table` for pyestat's structural reading:

```python
exp = client.explain_table(stats_data_id)
exp.role_pattern    # e.g. ('meta-axis', 'category', 'area', 'time')
exp.coverage        # 'builtin' | 'user' | 'project' | 'generic' | 'fallback'
exp.proposed_rule   # a RuleV2 starting point, or None
for axis in exp.meta.class_objs:        # facts (already fetched, no second call)
    print(axis.id, axis.name, exp.roles[axis.id].role)
```

`coverage` says what the auto path already does for this table:

- `builtin` / `user` / `project` — a specific rule already structures it.
- `generic` — no specific rule, but the auto path structures it from the axis
  roles; `proposed_rule` shows what it produces.
- `fallback` — too ambiguous or not flatly structurable; the auto path preserves
  raw rows (lossless), and a hand-written rule turns them into clean columns.

Start from `exp.proposed_rule` when it is present and refine it *with* the user;
when it is `None` (a `fallback` table, or one no generic rule fits), build the
rule from the `role_pattern` and axis roles `explain_table` reported. For the
members a rule references, reuse the `meta` you already read in step 2 —
`explain_table` returns the same metadata on `exp.meta`, so either works. What
you refine follows from why the auto output missed (step 4):

- **Two same-role axes collapsed** → give each its own column with `source.axis`.
- **A measure spread down rows** → pivot the `meta-axis` with `where` / `key`.
- **Unhelpful column names** → set each output `column` name. The per-value
  labels are e-Stat's own, surfaced as `*_label` by `to_flat()`; a rule cannot
  remap a code to a custom label (e.g. `110` → `male`).

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

A malformed rule raises a typed error at client construction (a `RuleLoadError`;
catch it via the public `EstatError`) — fix the YAML the message names and retry.

### 6. Confirm

Show the final data (or the saved rule + a sample of its output) and confirm it
matches the intent from step 1. If a rule was saved, tell the user it lives in
`./pyestat_rules/`, applies automatically, and can be shared via git.

## Error handling

- **Transient e-Stat error** ("データベースにアクセス中にエラー…時間をおいて"): retry after
  a short wait; do not treat it as a code failure.
- **`TooManyRowsError`**: the fetch is too large — narrow with `select`, or pass
  `max_rows=` when the user accepts a partial fetch.
- **No matching data** (`EstatApiError`, `status == 1`, 「正常に終了しましたが、該当
  データはありませんでした」): a valid `select` matched zero rows — not a bug.
  Widen or change the conditions. Genuine errors carry `status >= 100`.
- **Malformed rule file** (a `RuleLoadError`, caught via the public
  `EstatError`): a saved/edited rule file is malformed; fix the file the error
  names.
- **Missing `ESTAT_APP_ID`**: ask the user to set it; never fabricate one.
- **Goal integrity**: if a fetch fails, say so — never present fabricated or
  stale data as if the table were structured successfully.
