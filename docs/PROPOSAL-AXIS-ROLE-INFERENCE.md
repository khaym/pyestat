# Proposal: Axis-role inference

**Cut pyestat's rule count from O(tables) to O(role patterns) by
inferring each axis's role from table metadata, so the library scales
to the long tail of e-Stat tables — not just the high-traffic ones we
wrote rules for.**

- **Status**: Accepted (2026-06-02) — original six open questions closed;
  impact reflected into tasks #4 / #8 / #10 / #13 / #15. Amended 2026-06-02
  with Open question 7: the classifier may read fetched data rows (its
  signature gains an optional `rows` input), which retires the
  measure-spread lexicon as the load-bearing meta-axis signal.
- **Last revised**: 2026-06-02
- **Reader**: pyestat maintainers and future contributors who need to
  understand the proposed pivot away from per-table rules, or who hold
  a task this proposal touches (#4, #8, #10, #13, #15).

This document is the proposal. ARCHITECTURE.md and DESIGN.md continue
to describe the current MVP implementation until this proposal is
accepted and a separate task lands the updates.

## Summary

The field survey (CATALOG.md in `work/research/`) showed that a single
e-Stat `statsCode` binds to up to 30 structural groups, and within a
group axis names drift in two distinct ways. A per-table rule strategy
therefore scales as
`rules = (structural groups) × (semantic variants) × (value-spread
patterns)` — combinatorial in the number of e-Stat tables. With e-Stat
hosting tens of thousands of tables, manual rule authoring cannot keep
up.

Two terms used throughout: *role pattern* = the ordered tuple of axis
roles a classifier assigns to a table (see Role vocabulary below).
*Meta-axis* = an axis whose values are themselves column names of one
logical record (trade's `cat02` is the canonical example).

This proposal restructures the rule layer as a four-layer resolution
— generic role-based defaults (A) → built-in table-specific rules (B)
→ project-local rules (C) → heuristic fallback (D) — built from three
new pieces:

1. **Axis classifier** (feeds Layer A): a new component that observes
   table metadata and labels each axis with a role (`time`, `area`,
   `value`, `unit`, `category`, `meta-axis`, `aggregate`, `unknown`).
2. **Role-pattern matcher** (selects rules across A / B / C): rules
   match on the pattern of roles in a table, not the axis IDs.
3. **Output-schema-first rules** (format for B / C): a rule declares
   the desired output columns directly, with shorthand that lets
   unspecified columns inherit role-default transforms.

Expected effects:

- Rule count compresses from `O(tables)` to `O(role patterns)` for the
  generic layer; specific rules now carry semantic intent rather than
  structural plumbing.
  - As a result, LLM-agent workflows on uncovered tables produce
    structured output via Layer A or Layer D, instead of returning raw
    rows.
- Rule authoring (Skill #8) shifts from "describe the table structure"
  to "edit the proposed output schema."

## Background and motivation

### What the field survey showed

CATALOG.md records the 2026-05-24/25 survey of six statsCodes. Three
findings drive this proposal:

- **Structural multiplicity**: 1 statsCode contains 1–30 distinct
  axis-id signatures (CPI: 1, wage structure: 30). `StatsCodeMatcher`
  alone over-matches.
- **Name drift in two flavors**:
  - *Informative drift* (CPI base year, time granularity): the name
    encodes meaning the matcher should use. Substring matching helps;
    `name_digest` destroys the signal.
  - *Semantic drift* (wage `cat01` is "勤続年数" or "業種"): the name
    signals "different table." `name_digest` works; substring matching
    cannot scale.
- **Value spread in three patterns**: `tab` axis as meta, `cat02` axis
  as meta, or split across separate statsDataIds.

Together these defeat both the "one rule per statsCode" and the "one
matcher strategy fits all" assumptions baked into the MVP.

### Why current Matcher tuning isn't enough

The natural extension to the MVP matcher — adding `axis_names` to the
fingerprint — *increases precision* (fewer false positives) but
*decreases reach per rule* (each rule covers fewer tables). The rule
count grows, not shrinks. Precision tuning alone cannot fix a
combinatorial scaling problem.

### Why this matters under the broadened use cases

USE_CASES.md now treats LLM agents as a primary use case alongside
finance, real estate, research, and personal financial modeling. An
LLM agent calling `getStatsList` and landing on a table for which no
rule was written is the default scenario, not the exception. Without a
generic fallback that still produces structured output, the library's
value to LLM agents is bounded by the rules the maintainers happen to
have written.

## Proposed architecture

From here, the document speaks from the engine's perspective: what the
rule layer does at request time.

### Four-layer resolution

```
Layer A — Generic (built-in, automatic)
    Axis classifier → role pattern → role-default transformers.
    Covers any table where classifier confidence is sufficient.

Layer B — Specific (built-in, hand-authored)
    Output-schema-first rules for high-traffic tables (CPI, GDP, trade …).
    Carries domain-specific semantics (label maps, units, pivot specs)
    that Layer A cannot infer.

Layer C — Specific (project / user-authored)
    Same format as Layer B, dropped into project-local YAML.
    Picked up automatically (#15).

Layer D — Heuristic fallback
    Triggered when Axis classifier confidence is low.
    Returns rows with raw axis IDs and best-effort time / area parsing.
    Preserves data; does not normalize axes.
```

Resolution order: **C > B > A > D**.

### Axis classifier (new component)

The classifier observes a table's metadata and assigns each axis a
role from the controlled vocabulary below.

**Role vocabulary (resolved, see Open question 2).** The vocabulary
operates at *two levels* that the initial flat list conflated. An
**axis role** answers "what is this whole axis?" — exactly one per
axis, and what the classifier assigns. A **code/value attribute**
answers "what is this individual code or cell?" — detected per code,
orthogonal to the axis role, because such facts are held differently in
the wire format and so need different detection (see below).

*Axis roles* (classifier assigns one per axis):

| Role | Meaning | Example |
|---|---|---|
| `time` | Time axis (any granularity) | `time` axis with `2022000101` codes |
| `area` | Geographic / regional axis | `area` axis with JIS codes |
| `value` | Primary observation value | the cell (`$`) extracted as the record's value |
| `category` | Classification / dimension axis | wage age bracket, household goods type |
| `meta-axis` | Pivot meta-axis (one logical record split across N rows) | `tab` (表章項目) when n>1; `cat02` in trade |
| `unknown` | Classifier could not decide | triggers Layer D fallback |

*Code/value attributes* (detected per code; not axis roles):

| Attribute | Meaning | Where it lives in the wire format | Output role |
|---|---|---|---|
| `unit` | Unit-of-measure of a value | an attribute of a value-type entry (CPI/wage/GDP `tab` carries `@unit`), **or** a meta-axis row whose cell is a unit string (trade `単位` → `ＮＯ`) | *projection* — surfaces as an output column at conversion time |
| `aggregate` | Total/parent vs leaf detail | a code's position in the `level` / `parentCode` hierarchy, sometimes needing a name heuristic (`全国` / `総合` / `合計`) | *selection* — a per-row flag used to filter, not a column |

**Responsibility split (so demoting `unit` / `aggregate` does not break
pivot).** The classifier judges *structure* — is an axis a `meta-axis`
(does it split one record across rows)? — not unit semantics. That
structural label is what triggers pivot. The conversion definition
(rule) then supplies the *semantics* — which meta-axis value maps to
which named, typed output column. The `unit` attribute is consumed
downstream (authoring-time schema suggestion in Skill #8; routing
unit-valued meta rows to string columns in a Layer A generic pivot);
`aggregate` is consumed as a row filter. If the classifier cannot place
a `meta-axis` with confidence, the table degrades to Layer D (raw rows
preserved) rather than mis-pivoting. Dependency is one-way: classifier
→ conversion definition.

The flow below traces the trade table through that split — the
classifier decides *whether* to pivot (structure), the rule decides
*how* (semantics), and the two code attributes feed in where they are
used:

```mermaid
flowchart TD
    raw["Wire rows — one logical record split across N rows<br/>cat02=単位 → $ = ＮＯ<br/>cat02=数量 → $ = 12345<br/>cat02=金額 → $ = 98765"]
    raw --> clf["Axis classifier · heuristic · runtime<br/>judges STRUCTURE<br/>time / area / value / category / meta-axis (+ confidence)"]
    clf --> gate{"meta-axis placed<br/>with confidence?"}
    gate -- "no" --> layerD["Layer D · raw rows preserved<br/>no pivot — data not lost"]
    gate -- "yes" --> rule["Conversion definition (rule) · authoring-time<br/>supplies SEMANTICS<br/>単位→unit · 数量→quantity · 金額→amount_jpy"]
    rule --> pivot["Pivot engine<br/>group by non-meta axes → one row per group"]
    pivot --> out["Structured output<br/>{ cat01, area, time, quantity, amount_jpy, unit }"]
    unitAttr["unit · code attribute · projection"] -.->|"routes its value into a column"| rule
    aggAttr["aggregate · code attribute · selection"] -.->|"filters total vs detail rows"| pivot
```

The exact output shape (`unit` column naming, how `aggregate` is
carried) is deferred to the conversion-definition format and #10
(pivot); this question fixes only the vocabulary level.

**Inputs the classifier may consider:**

- `axis_id` (often hints at role: `time`, `area` are conventional)
- axis raw name
- `CLASS` vocabulary on the axis (does it look like an ISO 8601 date set? a JIS region set?)
- `TABLE_INF.TITLE_SPEC` prefix
- sibling axes' roles for disambiguation (optional second pass)
- the **fetched data rows** — per axis member, are the cells numeric or a
  unit string? This is the signal that places the non-`tab` `meta-axis`
  (see Open question 7); it is metadata-absent and request-time only,
  still deterministic.

**Implementation strategy (resolved, see Open question 1):** the
classifier is **deterministic heuristic at request time** — no LLM call
on the data path. Most roles fall out of e-Stat conventions and CLASS
code shape (`time` / `area` from conventional `axis_id` + date/JIS code
sets; `aggregate` from `parentCode` / `level`; `value` from cell type;
`category` by elimination). The hard role is `meta-axis`; two structural
signals place it without a keyword list — the `tab` convention and a
data-row unit-string signal (see Open question 7) — and an axis neither
can place keeps low confidence and routes to Layer D rather than to a
runtime model. LLM assistance is confined to authoring time (Skill #8), where it proposes
an output schema a human reviews and saves as a durable Layer B / C
rule — so an ambiguous table is resolved once, not re-inferred on every
call.

### Output-schema-first rule format

A Specific rule declares the **output columns** the caller will
receive, not the input table structure.

Long form:

```yaml
schema_version: "2"
match:
  role_pattern: [time, area, value]   # any table classified as time+area+value
output:
  - column: time
    source: { role: time }
    transform: iso8601
  - column: area
    source: { role: area }
    transform: jis_x_0401
  - column: value
    source: { role: value }
    transform: float
```

Short form (unspecified fields fall back to role-defaults from
Layer A):

```yaml
schema_version: "2"
match:
  role_pattern: [time, area, value]
output:
  - column: time
  - column: area
  - column: value
```

The two forms are isomorphic; the short form is sugar over the long
form, expanded at rule-load time using Layer A's role-default registry.

**Pivot via role + where predicate** (cat02-as-meta example):

```yaml
output:
  - column: time
  - column: area
  - column: cat01
  - column: unit
    source: { role: meta-axis, where: cat02 == "単位" }
  - column: quantity
    source: { role: meta-axis, where: cat02 == "数量" }
  - column: amount_jpy
    source: { role: meta-axis, where: cat02 == "金額" }
```

A `where` predicate on a `meta-axis` source triggers pivot expansion:
the engine groups rows by the non-meta axes and emits one output row
per group.

**Future extension point**: complex table-level transforms
(cross-table join, multi-axis pivot, conditional filters) that don't
fit the per-column model are reserved for a future `transforms:`
section. MVP does not include it.

### Resolution flow

1. Caller invokes `get_stats_data(stats_data_id)`.
2. Layer 2 (Endpoint) fetches the table and metadata.
3. Axis classifier runs on the metadata. Each axis gets a role and a
   confidence score.
4. If any required axis is `unknown` or its confidence tier is below
   threshold (see Open question 5) → Layer D fallback path.
5. Otherwise, the role pattern is computed.
6. Rule resolution walks C → B → A:
   - C / B: find a rule whose `match.role_pattern` matches.
   - A: build a default rule from the role-default registry.
7. Rule is applied; transformers run; rows emitted.

## Relationship to existing MVP

### What stays

- **HTTP I/O and Endpoint layers** (ARCHITECTURE Layer 1 / 2)
  unchanged. The proposal is contained within Layer 3.
- **Three storage layers** (DESIGN.md Decision E: user > project >
  builtin) carry over as Layer C and Layer B.
- **Skill #8** continues to exist; only its responsibility shifts.
- **Python-callable escape hatch** for transforms (DESIGN.md Decision C)
  remains available within rule definitions.

### What changes

- **Matcher Pipeline** (ARCHITECTURE 3a): `StatsCodeMatcher` +
  `FingerprintMatcher` are replaced by Axis classifier + role-pattern
  matching. Existing matchers can be retained as a fast-path
  optimization but are no longer authoritative.
- **Rule schema** (DESIGN.md Decision D): the axis-id-keyed structure
  (`axes.time.id`, `axes.area.id`, …) is replaced by output-column
  declarations (`output: [...]`). `schema_version` increments to `"2"`.
- **Transformer Pipeline** (ARCHITECTURE 3b): built at rule-load time
  from the rule's `output` declaration plus Layer A's role-default
  registry, instead of from a fixed expansion table.
- **DESIGN.md Decision A** (Hybrid matching) is superseded; the
  structural fingerprint becomes one signal feeding the Axis
  classifier rather than the matching authority.
- **DESIGN.md Decision B** (No-rule behavior): `rule="auto"` becomes
  "walk C → B → A then fall to D"; `rule="heuristic"` becomes an alias
  for direct Layer D invocation.

### Migration path

**Hard cutover, no compatibility layer** (resolved — see Open question
3). v1 was never published, so there is no installed base to stay
compatible with: the three built-in rules are rewritten to v2 in the
same change-set that introduces v2 and flips the loader's
`_SUPPORTED_VERSIONS` to `{"2"}`. v2 is the first publicly released rule
schema — an ordering dependency on #5 (OSS publish). The loader's
version-gating seam is retained but dormant until the first post-release
migration.

## Impact on existing tasks

| Task | Subject | Impact under this proposal |
|---|---|---|
| #4 | Standard codes (ISO 3166, JIS X 0401, ISO 5218, ISO 8601) | Becomes the implementation of Layer A's role-default transforms (`time → iso8601`, `area → jis_x_0401`, …). Scope unchanged; the integration point is the role-default registry. |
| #8 | Rule-authoring Skill | Responsibility shifts. The Skill generates the *initial output schema* by running the Axis classifier and presenting the inferred role pattern; the user edits column names and label maps, then saves. The "fill in the YAML template" workflow is replaced. |
| #10 | Generic pivot | Implemented as `meta-axis` role + `where` predicate inside `output:` (or, for complex cases that can't fit the per-column model, via the future `transforms:` extension). Pivot is no longer a standalone Layer 4 use case. |
| #13 | `name_digest` use | Axis names feed the Axis classifier as one signal (covering both informative and semantic drift). The original question "should we use `name_digest`?" becomes "how should the classifier weight axis name in role inference?" — already answered: both substring matching (informative) and digest comparison (semantic) are inputs the classifier needs. |
| #15 | Project-local YAML auto-discovery | Scope unchanged. The discovered files are output-schema-first rules instead of axis-id rules. |

## Open questions

Each closes independently; none block stating the proposal.

1. **Axis classifier implementation strategy** — *Resolved
   (2026-05-31)*: **deterministic heuristic at request time; LLM only at
   authoring time (Skill #8).** The data path stays LLM-free, so role
   inference is deterministic and unit-testable, the library carries no
   API-key/network dependency, and ambiguity is resolved once into a
   durable rule rather than re-paid per call. Axes the heuristic cannot
   place with confidence route to Layer D (raw rows preserved), not to a
   runtime model. Rejected: runtime hybrid (per-call LLM cost, latency,
   non-determinism, and result not persisted — duplicates the Skill #8
   rule-minting path). This constrains Open question 4 (Skill #8 UX):
   the LLM assist lives at authoring time, not request time.
2. **Role vocabulary completeness** — *Resolved (2026-05-31)*: the
   initial eight-role flat list **conflated two levels**. Re-leveled to
   six **axis roles** (`time`, `area`, `value`, `category`,
   `meta-axis`, `unknown`) plus two **code/value attributes** (`unit`,
   `aggregate`) that are detected per code, not assigned per axis — the
   2026-05 survey shows unit and aggregate are held differently in the
   wire format (unit as a value-type attribute or meta-axis row;
   aggregate as `level`/`parentCode` hierarchy) and so need different
   detection from axis roles. Speculative additions rejected:
   `quality-flag` (provisional/final) and a `time-instant` /
   `time-period` split have **zero instances** in the six surveyed
   statsCodes; growth is observation-driven, with `unknown` → Layer D
   absorbing the unforeseen until a concrete table demands a new role.
   See the re-leveled Role vocabulary section above for the
   responsibility split that keeps pivot detection intact.
3. **Migration path for existing axis-id rules** — *Resolved
   (2026-06-02)*: **hard cutover, no compatibility layer.** The decisive
   fact is that v1 was **never published** — no git tag, `version =
   "0.1.0"`, and OSS release is still pending (#5). The three options in
   the original question all presuppose an installed base that does not
   exist:
   - *v1→v2 load-time adapter* — **rejected**: a compatibility layer
     (two parsers, a migration table, tests for both, ongoing drift)
     serving a population of zero. Premature.
   - *v1 grace period* — **rejected**: a grace period means something
     only if v1 shipped and callers depend on it; neither holds.
   - *Rewrite bundled v1 rules in v2* — **adopted**, with one sequencing
     correction: the three built-in rules (`gdp_advance`,
     `population_estimates`, `foreign_trade`) cannot be rewritten into v2
     *before any implementation*, because v2's short form expands against
     Layer A's role-default registry. They are rewritten **in the same
     change-set that introduces v2 and flips `_SUPPORTED_VERSIONS` from
     `{"1"}` to `{"2"}`** in the loader. The loader's existing
     `schema_version` gate is the single cutover point: a stray v1 file
     then fails fast with the error it already raises for unknown
     versions.

   Consequences:
   - **No v1 reaches a public release; v2 is the first published rule
     schema.** This imposes an ordering dependency on #5 (OSS publish):
     publish after v2 lands, or never tag v1. (The internal transition
     may briefly accept `{"1", "2"}` so the test suite migrates
     incrementally, but the shipped artifact supports only v2.)
   - **#15** (project-local rule discovery) loads **v2 only** from day
     one — it never needs a v1 code path.
   - The loader's migration seam (documented as letting "a future
     migration step sit between the raw mapping and the validator") is
     **kept but stays dormant**: its first real use is the post-1.0
     v2→v3 migration, when a published installed base finally makes an
     adapter / grace period earn its keep. The chosen strategy is
     therefore not "migrations are unnecessary" but "the first migration
     that needs compatibility machinery is the first one *after* release,
     not this one."
4. **Skill #8 UX in detail** — *Resolved (2026-06-02)*: close the
   *direction*, defer the *detail*. The two sub-options are not equal —
   the rest of this proposal already decides between them. *Static
   template + LLM completion* is the "describe the table structure / fill
   in the YAML template" workflow this proposal explicitly supersedes
   (see Summary and the #8 impact row); reintroducing it would undo the
   pivot. **Adopted direction: interactive review of classifier output**
   — the Skill runs the Axis classifier, presents the inferred role
   pattern and a proposed v2 output schema, and the human edits column
   names / label maps and saves a durable Layer B / C rule. This is the
   skeleton Open question 1 already forces (LLM assist at authoring time,
   classifier-proposed, human-reviewed, persisted once).

   **The detailed UX is deliberately deferred to #8 implementation,
   after the classifier exists.** The concrete flow — how the role
   pattern is surfaced, how edits and label-map elicitation are captured,
   what preview / confirmation looks like — is *data-dependent*: it can
   be designed well only against the classifier's real output shape and
   with the v2 loader as the save target. Specifying it now would be
   speculative. So #8's detailed design is **blocked-by the Axis
   classifier and v2 rule-loader implementation tasks** (created at
   acceptance). Q4 closes by fixing the skeleton, the direction, and that
   dependency — not by inventing UX in a vacuum. This resolves the
   "architecture-neutral but blocks #8 task design" tension explicitly:
   the architecture-neutral part (direction) is decided here; the
   #8-blocking part (detail) is sequenced after implementation.
5. **Confidence threshold for Layer A → Layer D transition** —
   *Resolved (2026-06-02)*: confidence is a **discrete tier (`high` /
   `medium` / `low`)**, not a calibrated probability — Open question 1
   made the classifier a deterministic heuristic, which has no softmax
   to read a 0–1 score from. A tier records **how many independent
   signals agreed** on an axis's role:
   - `high` — the role's defining signals concur (e.g. `time`:
     conventional `axis_id` *and* CLASS codes parse as e-Stat date
     codes).
   - `medium` — one strong signal, the rest silent or neutral
     (conventional `axis_id` but codes don't cleanly parse, or vice
     versa); `category`-by-elimination tops out here.
   - `low` — signals conflict, or only the weakest evidence is present;
     the typical `meta-axis` miss lands here.

   The gate is **per-axis tier, aggregated weakest-link to a
   table-level decision**: every axis the matched role pattern
   *requires* must clear the threshold, else the whole table falls to
   Layer D (this is Resolution flow step 4). **Default threshold =
   `medium`** (only `low` routes to D); its concrete calibration is
   Open question 6's job — Q6 is the loop that may move this dial, Q5
   fixes only the mechanism. Consistency check: at `medium`,
   `category`-by-elimination passes (it is a legitimate Layer A
   dimension role, not a fallback), while an unreadable `meta-axis`
   (`low`) correctly drops to D rather than mis-pivoting. **Rejected for
   MVP**: per-axis independently-tunable thresholds (no evidence any
   axis needs a different bar yet — premature). **Reserved, not MVP**: a
   per-rule `require_confidence:` override on Layer B / C rules (a rule
   author demanding `high` before their rule fires); the MVP default is
   the single global table-level gate.
6. **Layer A coverage measurement** — *Resolved (2026-06-02)*: a
   **checked-in evaluation harness** (extending
   `work/research/analyze.py`) scored against a **hand-labelled gold
   set** — the role of every axis in a *representative sample per
   structural group* across the six surveyed statsCodes (sample, not
   census: the catalog already flags wage-structure's 30 groups and
   GDP's 16k tables as too large to enumerate). Two metrics, reported as
   a pair because either alone is gameable:
   - **Reach** — axis-signatures Layer A classifies (does not route to
     D) ÷ total. A *coverage* statistic, reported but **not gated** (an
     overconfident classifier maximises reach by mislabelling).
   - **Role accuracy** — axes whose inferred role equals the gold role ÷
     axes over the reached set. This is the *gate*.
   - **Mis-pivot guard** — `meta-axis` false positives tracked
     separately and weighted hardest: pivoting a table that shouldn't be
     pivoted corrupts data silently, so the bar is **zero false
     meta-axis on the gold set** — better to drop to D than mis-pivot
     (mirrors the Responsibility-split section).

   **"Good enough" bar**: role accuracy on the reached set ≥ a target
   (start strict, tune with observation) **and** zero mis-pivot, with
   reach reported alongside. **Relationship to Q5**: the harness sweeps
   Q5's threshold over {`low`, `medium`, `high`}, traces the
   reach-vs-accuracy curve, and picks the lowest threshold that still
   clears the correctness bar — so Q6 *calibrates* the knob Q5
   *defines*.

   **Coverage as a living loop** (not a one-shot gate): every **Layer D
   fall** and every **Skill #8 authoring event** records the table's
   structural fingerprint plus which axis was `unknown` / low-confidence.
   That log is the growth engine the static survey lacks — it (a) feeds
   new entries into the gold set from real field encounters, so the
   measurement set widens with usage instead of staying frozen at six
   statsCodes; (b) makes Open question 2's deferred "a concrete table
   demands a new role" trigger observable in practice rather than in
   principle (Layer D otherwise silently absorbs the very signal that
   Layer A needs to grow); and (c) supplies CATALOG.md's "not yet
   classified" section. **A recurring fingerprint in the D-sink is the
   promotion signal**: a structural pattern that repeatedly falls to D —
   or is repeatedly hand-resolved the same way in Skill #8 — is the
   evidence to widen *built-in* coverage, either by adding a Layer B rule
   or by extending the classifier's signals / Layer A role-default
   registry. So testing across many tables does not merely measure
   Layer A — it grows it. The instrumentation point coincides with
   Skill #8 (Open question 4), where a human already resolves a D-fall
   into a durable rule.

   **Honest scope**: passing on six statsCodes is a
   **regression floor, not proof of generalization**; the long tail is
   caught by Layer D + Skill #8, not by perfecting Layer A. **Rejected**:
   large automated surveys over thousands of tables (no gold labels →
   accuracy unmeasurable, and high-cardinality fetch is expensive — see
   CATALOG incidentals); unsupervised self-consistency metrics (blind to
   systematic mis-classification).
7. **Classifier input scope — metadata only, or metadata + data?** —
   *Resolved (2026-06-02, post-acceptance amendment)*: **the classifier
   may read the fetched data rows, not just metadata.** The data path
   stays deterministic — no LLM (Open question 1 is unchanged); reading
   cell values is not inference.

   The decisive evidence is a field PoC over the six surveyed statsCodes
   (`work/research/poc_meta_score.py`). It tested whether the non-`tab`
   `meta-axis` — trade's `cat02` = 数量 / 金額 / 単位, the case the
   measure-spread lexicon was carrying — can be placed *without* a keyword
   list (the measure-spread lexicon being the 数量 / 金額 / 単位 / 価額
   keyword set):
   - **The reliable signature is in the data, not the metadata**: a member
     whose cells are a genuine *unit string* (trade 単位 → `ＮＯ`) coexisting
     with numeric members. e-Stat leaves `@unit` unset on trade's 数量 /
     単位 members, so metadata alone cannot see this.
   - **Metadata `@unit` heterogeneity is not a usable trigger**: population's
     男女別 axis carries heterogeneous `@unit` (千人 / 女＝１００) yet is a
     category — keying on it mis-pivots (the mis-pivot guard, Q6).
   - **Suppression markers are the confounder**: e-Stat's "-" / "***" cells
     are non-numeric but carry no unit meaning, so they must be excluded
     before the string-vs-number test (household samples are ~90 % "-",
     which faked heterogeneity on `time` until markers were dropped).

   On the six statsCodes the data signal flagged trade's `cat02` and **zero**
   false positives — age, 男女別, area, time, and every dimension stayed
   non-meta.

   Consequences:
   - The **measure-spread lexicon (数量 / 金額 / 単位 / 価額) is retired as the
     load-bearing meta signal.** Non-`tab` meta detection rests on two
     structural signals, neither using a keyword list: the `tab` convention
     for all-numeric value-type metas, and the data unit-string signal for
     unit-row metas. The lexicon may survive only as a weak metadata-only
     fallback (capped at `medium`) on a data-absent path.
   - The classifier signature gains an **optional `rows` input**. With rows,
     it runs the unit-string signal; without (e.g. a `getMetaInfo`-only
     rule-validation path), it degrades to the metadata-only heuristics.
   - **Rejected**: a runtime LLM over the data (Open question 1 already
     rejected this — the signal is a deterministic type check, not
     inference); metadata `@unit` as the meta trigger (population
     false-positive above).

   Relationship to Q6: the coverage harness now measures this data signal's
   reach and its zero-false-meta bar; a recurring D-fall that the signal
   *could* have placed is a promotion candidate.

## Out of scope

- **Updates to ARCHITECTURE.md and DESIGN.md.** A separate task
  promotes accepted parts of this proposal into those documents.
- **Implementation task decomposition.** Tracked separately in
  task-tracker after acceptance.
- **Public API surface changes** beyond `rule="auto"` semantics. The
  `EstatClient` method signatures stay backward-compatible.
- **Full coverage of every e-Stat table.** Aligns with USE_CASES.md:
  high-traffic tables in Layer B, long tail via Layer A + Skill +
  Layer D.

## Next steps

Accepted 2026-06-02. Remaining work, tracked separately in task-tracker:

1. Decompose implementation tasks: Axis classifier, role-default
   registry (#4), output-schema-first v2 rule loader, Layer D heuristic
   mode, Skill #8 refactor (#8), D-sink instrumentation (Q6).
2. Promote accepted parts into ARCHITECTURE.md and DESIGN.md (separate
   task; the as-built MVP docs stand until then).
3. Honour the ordering dependency recorded on #5: v2 is the first
   published rule schema — do not tag / publish v1.
