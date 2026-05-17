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

The fingerprint is the set of axis `@id` values plus a stable summary
of axis names. The narrow-then-validate order avoids the false
positives of fingerprint-only matching and the brittleness of
`statsDataId`-only matching.

### B. No-Rule Behavior

**Decision: Layered fallback.**

- `rule=None` — raw mode. Rows are returned as `axis_id`-keyed dicts
  with original codes; no normalization. Always works.
- `rule="auto"` — heuristic mode. The library substitutes labels
  using CLASS_INF and applies safe defaults (labeled keys instead of
  axis IDs). No standard-code mapping and no aggregate exclusion.
- Explicit `rule=...` — full transformation per the rule.

This avoids forcing rule authoring upfront while preserving the
value of rules when they are present.

### C. Rule Description Format

**Decision: Declarative YAML with a Python callable escape hatch.**

Rationale:

- YAML is diff-reviewable, generatable by the Phase 2 authoring Skill
  (task #8), and accessible to non-engineers.
- A Python callable escape hatch (e.g. `value_transform: !python ...`)
  covers cases the declarative form cannot express.

### D. Rule Scope (OPEN — to be resolved during #7)

**Direction: Start minimal, expand stepwise.**

Two framings were considered:

1. Bake processing hints into the schema upfront for downstream
   convenience.
2. Start with a minimum-viable schema and grow it based on actual
   rule-authoring experience.

The user chose (2). Proposed minimum-viable set:

- **Required**: declare which axis is `time`, which is `area`;
  declare the value type.
- **Optional (initial)**: axis rename, label augmentation, aggregate
  exclusion, standard-code mapping, value transformation.

The concrete MVP schema and the expansion roadmap will be finalized
inside task #7 when the engine is being built. Each new field added
should be motivated by an actual rule that needs it.

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

## Open Questions (Carried to #7)

These were not resolved during the Phase 0 discussion and must be
decided during #7's design phase before implementation:

- **Execution model**: sync only, async only, or both? If both, at
  which layer is the sync/async split made?
- **HTTP behavior**: retry policy (e.g. exponential backoff), default
  timeout, progress reporting for multi-page fetches.
- **Pagination UX**: R-style auto-fetch-all by default, or explicit
  `limit` / `start_position`? Iterator vs. eager list.
- **Endpoint coverage**: which endpoints ship in the rewrite —
  `getStatsData` (JSON), `getSimpleStatsData` (CSV), `getMetaInfo`,
  `getStatsList`, `getDataCatalog`?
- **Concrete MVP rule schema**: which fields land in the initial
  release vs. the deferred-to-later list (Decision D's stepwise
  roadmap).

## Related Tasks

- **#4** — Standards code catalog (JIS X 0401/0402, ISO 5218,
  ISO 8601, ISO 3166).
- **#5** — OSS release audit.
- **#7** — Rule-driven rewrite (umbrella).
- **#8** — Rule authoring Skill (Phase 2).
