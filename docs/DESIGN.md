# pyestat Design Decisions

Captured: 2026-05-17. Source discussion: task #6 (closed).
Drives implementation in task #7.

## Status

Phase 0 shipped a synchronous JSON client for `getStatsData` only.
Inspecting three representative tables showed that this implementation
is built on assumptions real e-Stat responses violate. Task #7 is a
full redesign rather than an incremental fix.

## Context: Observed Table Variance

Three tables were sampled to test the original "uniform parser"
assumption:

| Table | statsDataId | statsCode | Rows | Axes |
|---|---|---|---|---|
| Population estimates (monthly) | 0003443838 | 00200524 | 4,293 | tab, cat01–04, area, time |
| Quarterly GDP advance | 0003109741 | 00100409 | 2,816 | tab, cat01, time |
| Foreign trade (commodity by country) | 0004049306 | 00350300 | 3,828,581 | cat01, cat02, area, time |

Key structural differences that defeat a uniform parser:

- **Axis count varies** (3–7). `tab` and `area` are optional.
- **Label-less axes exist**: trade `cat01` uses HS commodity codes
  where `@code == @name`. No human-readable label is provided by
  e-Stat.
- **Per-row value type varies within one response**: in the trade
  table, `value` is a unit symbol (e.g. `"ＮＯ"`) when `cat02=110`,
  a quantity when `cat02=120/130`, and a monetary amount when
  `cat02=140`.
- **`TABLE_INF` schema drifts across tables**: `TITLE` is a
  `{@no, $}` dict in population, a bare string in GDP. `SURVEY_DATE`
  is `0` (number) or `"202510-202512"` (string). `DESCRIPTION` is
  empty string or dict.
- **`tab` isn't always the cell-meaning axis**: in trade, that role
  is played by `cat02`.
- **Axis `@id` is stable; axis `@name` is not**: `tab`, `cat01`,
  `time` are consistent across tables, but the names drift —
  `"時間軸（年月日現在）"`, `"時間軸（四半期）"`, `"時間軸(年次)"`
  mix full-width and half-width parentheses for the same concept.
- **Pagination is mandatory at scale**: trade has 3.8M rows; the
  default `getStatsData` response caps at 100,000 with `NEXT_KEY`
  for pagination.

## Reference: R `estatapi` Library

The R library was reviewed at `/tmp/estatapi/`. Notable choices:

- It uses the **CSV endpoint** (`getSimpleStatsData`) for data, not
  the JSON endpoint. CSV returns CLASS_INF joined into the row as
  paired `code` / label columns (e.g. `tab_code` plus `表章項目`),
  eliminating manual label substitution on the client.
- It splits data fetch across **three API calls**: `cntGetFlg=Y` for
  the row count, `getMetaInfo` for axis metadata, and
  `getSimpleStatsData` (CSV) for the data itself.
- It **auto-paginates** in 100,000-row chunks when `.fetch_all=TRUE`
  (the default).
- It exposes a **binary `.use_label` switch** — either codes or
  labels, not both.
- It delegates value type conversion to the CSV parser (`readr`).

Trade-offs against pyestat's LLM-oriented goals:

- CSV strips `@parentCode` / `@level` from CLASS_INF, so the
  aggregate-vs-detail hierarchy is lost.
- CSV labels for `time` and `area` are display-formatted
  (`"2022年1月"`, `"205_英国"`), which are harder to machine-process
  than the codes.
- Preserving hierarchy and mapping to standard codes (ISO 8601, JIS,
  ISO 3166) requires JSON plus custom transformation — favoring the
  JSON route for the engine even though the CSV route is simpler.

## Decisions

### A. Rule Matching

**Options considered:**

| Option | Pro | Con |
|---|---|---|
| `statsDataId` exact | Precise; no false matches | Breaks on table revisions; rule count proliferates |
| `statsCode` (family) | Reusable across the statistic family | The same family can include structurally different tables |
| Structural fingerprint | Reusable across same-shape tables | May falsely match unrelated tables |
| Hybrid | Combines safety and reuse | Slightly more complex matching logic |

**Decision: Hybrid — `statsCode` narrows the candidate set, structural
fingerprint validates the match.**

The fingerprint is the set of axis `@id` values plus a digest of
normalized axis names. Name normalization handles the observed drift
across tables (e.g. `"時間軸（年月日現在）"` vs `"時間軸(年次)"`):
inputs are NFKC-normalized (collapsing full-width / half-width
parenthesis variants), stripped of any trailing parenthesized suffix,
and lower-cased before being hashed — leaving a stable concept stem
(`"時間軸"`).

The narrow-then-validate order avoids the false positives of
fingerprint-only matching and the brittleness of `statsDataId`-only
matching.

### B. No-Rule Behavior

**Decision: Layered fallback with auto-resolution.**

The `rule=` parameter takes four forms:

- `rule=None` — raw mode. Rows are returned as `axis_id`-keyed dicts
  with original codes; no normalization. Always works.
- `rule="auto"` (default) — classifies the table's axes, then walks
  Layers C > B > A > D: a matching v2 rule (user > project > builtin,
  Decision E), else a generic rule derived from the classified roles
  (Layer A), else the Layer D heuristic when the table cannot be
  structured (a low-confidence axis, or a shape needing a pivot).
- `rule="heuristic"` — label substitution only. Each axis with a
  `CLASS` lookup gets an additive `{axis_id}_label` field alongside
  the raw code; axis-ID keys are preserved so downstream filters that
  work on raw codes keep working. No standard-code mapping and no
  aggregate exclusion.
- `rule=RuleV2(...)` — a single explicit rule applied directly,
  bypassing the resolution chain.

This avoids forcing rule authoring upfront while preserving the
value of rules when they are present. The default `"auto"` makes
the bundled rules' value reach the un-decorated `get_stats_data(id)`
call; the explicit `"heuristic"` exists for callers who want a
stable shape regardless of which built-ins ship in a given
pyestat version.

*Drift note (2026-05-21):* an earlier draft folded `"auto"` and
`"heuristic"` into a single mode. They were split during task #7
implementation so the default could pick up bundled rules
automatically without changing the semantics of explicit
`"heuristic"`. The output shape of `"heuristic"` also differs from
that draft: the original wording said "labeled keys instead of
axis IDs", but the implementation keeps both because dropping
axis-ID keys would silently break any downstream filter written
against the raw codes.

**Surfacing vs. degrading a failed rule (2026-06-06).** Resolving a rule
does not guarantee it applies: its role may be absent from the table, a
short-form column may not expand, or a column may name an unknown
transform. The split is one of *agency* — `"auto"` surfaces a failure the
caller can fix and degrades one they cannot:

| Failing rule's origin | On failure |
|---|---|
| An explicit `rule=RuleV2(...)`, or a matching user / project rule (Layer C) | Surface as a typed `EstatError` |
| A built-in rule (Layer B) | Degrade to the lossless Layer D |

Surfacing lets the caller correct their own rule and re-run; a
wrong-looking result they could not trace to their rule would be worse.
Degrading avoids raising an error they have no power to resolve — they
cannot edit a built-in. The two non-failure cases route by the same logic:
a generic Layer A rule uses only total role-default transforms, so it
cannot fail at apply time, and a classification too weak to trust produces
no rule at all — both go straight to Layer D.

An unknown transform name surfaces as `UnknownTransformError` (a typed
`EstatError`), never a bare `KeyError`, and follows the same split. So does
a same-layer *conflict* — two rules claiming one role pattern: a user /
project conflict surfaces as `AmbiguousRuleError`, while a built-in conflict
is skipped (falling through to a generic rule or Layer D), being a packaging
bug the caller cannot fix and one meant to be caught in CI. A declared
*strict* time format (`yearly` / `monthly_e_stat` / `quarterly_e_stat`) that
the table's codes violate is a `TimeFormatError` — an authoring decision (the
rule chose a format the data does not fit), so it routes by provenance like
the others rather than being silently re-normalized; the total
`best_effort_time` role-default never lands here (see Decision J). A
registered transform that *itself* raises at runtime — a genuine code bug,
not a format the author mis-chose — is a library bug and surfaces regardless
of origin; the callable escape hatch (Decision C), when added, will route its
runtime exceptions by this same rule.

### C. Rule Description Format

**Decision: Declarative YAML with a Python callable escape hatch.**

Rationale:

- YAML is diff-reviewable, generatable by the Phase 2 authoring Skill
  (task #8), and accessible to non-engineers.
- A Python callable escape hatch (e.g. `value_transform: !python ...`)
  covers cases the declarative form cannot express.

### D. Rule Scope

**Decision: Start minimal, expand stepwise. MVP schema confirmed
during #7 design phase (2026-05-20).**

Two framings were considered:

1. Bake processing hints into the schema upfront for downstream
   convenience.
2. Start with a minimum-viable schema and grow it based on actual
   rule-authoring experience.

The user chose (2).

**Required fields (MVP):**

- `schema_version: "1"` — the rule file's schema version. Additive
  expansions (new optional fields, new transformer keywords) keep
  this at `"1"`; breaking changes increment it and the Rule loader
  routes through a migration. See ARCHITECTURE.md for the loader
  contract.
- `match.statsCode` — narrows the candidate set (Decision A); the
  fingerprint is computed and validated by the engine and not stored
  in the rule file.
- `axes.time.id` — which axis `@id` carries time semantics.
- `axes.time.format` — name of a built-in time parser (see below).
- `axes.area.id` — which axis `@id` carries area semantics. Optional;
  some tables (e.g. GDP) have no area axis.
- `value.type` — `number` or `string`. Conditional (per-row variable
  type, as in the trade table) is deferred to the expansion list.

**Built-in time parsers shipped at MVP:**

| `format` value | Input shape | Output | Granularity |
|---|---|---|---|
| `monthly_e_stat` | 10-digit `YYYY00MMMM` (month digits repeated) | `"YYYY-MM"` | `monthly` |
| `quarterly_e_stat` | 10-digit `YYYY00<start_mm><end_mm>` (e.g. `0103` → Q1) | `"YYYY-Qn"` (convention) | `quarterly` |
| `yearly` | 10-digit `YYYY000000`, or bare 4-digit `YYYY` | `"YYYY"` | `yearly` |

Shapes were pinned against the live API during the #7 build (see
`tests/test_time.py`); earlier drafts of this table guessed 5-digit
quarterly and 4-digit yearly inputs, neither of which e-Stat actually
returns.

The output row preserves the raw code and adds the normalized value
and granularity:

```python
{
    "time_code": "2022000101",      # raw e-Stat code
    "time": "2022-01",              # normalized (ISO 8601 leaning)
    "time_granularity": "monthly",  # metadata for caller-side aggregation
    ...
}
```

*(Superseded by Decision J (#35): these fields are now nested in a single
`time` object — `{code, label, normalized, granularity}`. `to_flat()`
reproduces exactly the flat keys shown here.)*

ISO 8601 has no quarter notation; `"YYYY-Qn"` is a widely-recognized
convention preferred over the heavier `YYYY-MM-DD/YYYY-MM-DD` period
form. LLMs read it without difficulty.

**Why `area` is not in MVP:** the same need (cross-table region
joining) is acknowledged but observed variance in `area` is narrower
than in `time`. Promoted to MVP when a concrete rule needs it.

**Expansion candidates (added when an actual rule needs them, not
on speculation):**

| Extension | Motivating table | Sketch |
|---|---|---|
| `value.type: conditional` | Trade statistics | Per-row value type keyed off another axis (`cat02` → unit/quantity/amount) |
| `axes.<id>.exclude_aggregate` | All | Drop "total / aggregate" code rows |
| `axes.<id>.standard_code` | All | Map to ISO 8601 / JIS X 0402 / ISO 3166 (task #4) |
| `axes.<id>.rename` | All | `cat01 → commodity` for readability |
| `axes.area.format` | TBD | Same shape as `axes.time.format`; promote when needed |
| `value.transform: !python ...` | Edge | Callable escape hatch (Decision C) |

### E. Rule Storage Layers

**Decision: Three-layer resolution order.**

1. User-specified (passed explicitly to the client).
2. Project-local (`./rules/*.yaml` in the consumer's project).
3. Library-bundled (`pyestat/rules/builtin/*.yaml`).

Earlier layers override later ones. This mirrors common config
patterns (per-user > per-project > system) and lets users override
bundled rules without forking.

### F. Rule Authoring Skill (Phase 2)

**Decision: Defer to Phase 2.**

Tracked as task #8. The Skill should land after the engine and the
bundled rules ship in #7, so it has a stable generation target.

### G. Endpoint Coverage

**Decision: Three JSON endpoints — `getStatsData`, `getMetaInfo`,
`getStatsList`. Defer `getSimpleStatsData` (CSV) and `getDataCatalog`.**

| Endpoint | Included | Reason |
|---|---|---|
| `getStatsData` (JSON) | Yes | Core data path |
| `getMetaInfo` | Yes | Enables fingerprint validation (Decision A) and pre-fetch row-count checks without downloading data |
| `getStatsList` | Yes | Discovery is a frequent LLM / data-scientist workflow when `statsDataId` is unknown |
| `getSimpleStatsData` (CSV) | No | Context section ruled CSV out (hierarchy loss). Carrying both would double the rule surface area |
| `getDataCatalog` | No | No identified use case yet |

`cntGetFlg=Y` is a query option on `getStatsData`, not a separate
endpoint, and is used internally for the row-count safety check
(Decision I).

### H. Execution Model

**Decision: Sync only at MVP. Confine HTTP I/O so an async client
can be added later without touching transformation logic.**

Rationale:

- Primary users (Jupyter, LLM tool calls, scripts) want sync APIs.
- `getStatsData` pagination is `NEXT_KEY`-serialized, so async gives
  no speedup for the dominant slow case (large single-table fetches).
- Parallel multi-table fetches are achievable from caller code with
  `concurrent.futures`.
- httpx exposes symmetric sync/async APIs, so a thin `EstatHttpClient`
  (sync) can later be paired with `AsyncEstatHttpClient` reusing rule
  application, fingerprint matching, and page assembly verbatim.

### I. Pagination and HTTP Behavior

**Pagination:**

- `get_stats_data(stats_data_id, max_rows=None)` — fetches all pages
  by default (R-style). Pre-fetches the row count via `cntGetFlg=Y`;
  if `max_rows` is set and the table exceeds it, raises
  `TooManyRowsError` before downloading data.
- `max_rows=None` opts out of the safety check ("commit to fetching
  everything").
- `iter_stats_data_pages(stats_data_id)` — low-level page-at-a-time
  iterator for progress reporting and streaming use cases.

**HTTP:**

- *Timeouts*: `connect=10s`, `read=60s` (e-Stat is observed to be
  slow on large tables). Caller-overridable.
- *Retry*: 3 attempts with exponential backoff (0.5s → 1s → 2s) and
  jitter on 5xx, connection failure, timeout, and the transient 4xx
  codes 408 (Request Timeout) and 429 (Too Many Requests). Other 4xx
  responses and e-Stat logical errors (HTTP 200 with
  `RESULT.STATUS != 0`) fail immediately — they are deterministic,
  retrying wastes quota.
- *Progress*: optional `progress: Callable[[ProgressEvent], None]`
  parameter; `ProgressEvent` carries
  `{page, total_pages, rows_fetched, rows_total}`. No tqdm dependency
  — callers can route into tqdm or any other reporter.
- *Rate limiting*: e-Stat publishes no rate limit. No client-side
  inter-page sleep at MVP. If 429 is ever observed, the retry layer
  absorbs it.

### J. Output Contract — Nested Canonical Cells (2026-06-07, #35)

**Decision: one canonical *nested* record shape across every conversion
path, with a lossless flat projection on the side.**

Before #35 the `rule="auto"` paths disagreed on shape. Layer D added
`{axis}_label`, a normalized `time` plus `time_code` / `time_granularity`,
and kept `value` / `unit`; the generic v2 1:1 path emitted bare
`{axis: code}` columns — no labels, no time metadata, and (on a table with
no VALUE-role axis) no value at all. A caller could not write one piece of
code against the result.

Each field is now a self-describing object:

| Cell | Shape | Notes |
|---|---|---|
| dimension (category / area / tab) | `{code, label}` | A label-less code (trade HS, where `code == name`) carries its code as the label, so the cell is never partial. |
| time | `{code, label, normalized, granularity}` | Raw code, e-Stat display name, ISO-leaning normalized string, granularity tag. An unrecognised code keeps `normalized == code` and `granularity = None`. |
| measure (observation) | `{value, unit}` | Wrapping the value with its unit keeps a pivoted table correct when measures carry different units (trade's 数量 in ＮＯ vs 金額 in 千円), which a single shared `unit` sibling could not. |

The nested form is canonical because it is self-describing — an LLM agent
reads `row["cat01"]["label"]` without knowing a suffix convention — and
because nested → flat is a cheap, lossless projection whereas flat → nested
would require re-pairing by a fragile suffix rule.
`StatsDataResponse.to_flat()` gives pandas users the legacy
one-column-per-field shape (`cat01` / `cat01_label`; `time` / `time_code` /
`time_label` / `time_granularity`; `value` / `unit`);
`pandas.DataFrame(resp.to_flat())` is the DataFrame path, so no pandas
dependency is taken on.

A time column's *declared format* drives its `normalized` / `granularity`,
and the same format machinery serves every path so the object never drifts.
The role-default `best_effort_time` (what a short-form column and the generic
Layer A rule inherit) is **total** — it probes the parsers and keeps an
unrecognised code raw, never raising — so the no-rule paths (Layer D, generic)
cannot fail on a time code. A rule that *explicitly* declares a strict format
(`yearly` / `monthly_e_stat` / `quarterly_e_stat`) is honored: if the table's
codes do not match that shape, the mismatch is a typed `TimeFormatError`
routed by provenance (Decision B) — a caller-authored rule surfaces so they
can pick the right format, a built-in degrades to Layer D — rather than being
silently re-normalized by a best-effort guess the author did not ask for.
This keeps the agency split intact: the library's total default never
crashes, while a caller's explicit instruction is either applied as written
or surfaced for them to fix. `rule=None` (raw) is unchanged: Layer 2's flat
rows pass through, and `to_flat()` is a no-op on them.

**Scope boundary.** This decision fixes the *shape*. Two content gaps
follow it, tracked separately: a table with no VALUE-role axis (e.g.
quarterly GDP, `category + time`) still omits the observation value until
#33 makes it intrinsic; standard-code normalization of `area` is added as
an additive field by #4. Auto-generated pivots (#34) follow the measure
shape above.

## Open Questions (Carried to #7)

All resolved during the #7 design phase on 2026-05-20. See revised
Decision D and new Decisions G, H, I above.

## Related Tasks

- **#4** — Standards code catalog (JIS X 0401/0402, ISO 5218,
  ISO 8601, ISO 3166).
- **#5** — OSS release audit.
- **#7** — Rule-driven rewrite (umbrella).
- **#8** — Rule authoring Skill (Phase 2).
