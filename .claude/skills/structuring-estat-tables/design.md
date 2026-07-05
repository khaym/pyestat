# structuring-estat-tables Design Doc

*Reader: a maintainer checking whether a change fits this skill's design (the runnable procedure lives in [SKILL.md](SKILL.md)).*

## Purpose

Guide a user — who may not be a developer — from an e-Stat `statsDataId` plus an
intent ("I want annual CPI since 1990") to the data that actually serves that
intent, and, when the table's structure is not covered, to a durable conversion
rule. The hard part of e-Stat is rarely "no rule exists"; it is that a table's
real content surprises the user (a time axis mixing calendar and fiscal years,
aggregate rows double-counting detail, an unexpected value spread), so a
conversion rule alone does not complete the job. This skill makes those
surprises visible and walks the user to usable data.

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| Spine is **comprehension + intent-matching (P1)**; rule authoring (P2) is a terminal branch | The gist CPI case was already covered by `rule="auto"`; the residual barrier was noticing the calendar/fiscal mix and choosing a slice — not a missing rule. So comprehension, not authoring, is the default path. |
| The author-vs-narrow branch turns on **whether the `rule="auto"` output serves the intent, not on the `coverage` value** | A covered table (`builtin` / `generic`) can still fail the intent — most often two same-role axes (常住地×従業地, 建築主×用途) collapsing into one column, where the pattern matches but meaning is lost. Deciding by coverage alone would wrongly skip authoring for these. |
| **Main session** execution pattern | The skill is an interactive dialog that confirms axis meaning and intent with the user; it is not one-shot research. (skill-authoring selection flow: interactive → Main session.) |
| **Run the code for the user by default** | The user may not be a developer. Claude runs the pyestat calls, profiles the data, drafts any YAML, and reports in plain language for approval. Developers can still take the snippets. |
| **Hazards are diagnosed by observation, never hardcoded** | e-Stat's pitfalls are open-ended; enumerating them in code or a fixed checklist is non-extensible. The skill inspects the actual members and a data sample and reasons about what would surprise *this* intent. |
| **`get_meta_info` leads; `explain_table` is the authoring entry, deferred** | The common path reads axes and member codes from `get_meta_info` — the single source of the `select` vocabulary (keys = `class_obj.id`, values = member `code`). `explain_table` costs a data page plus classification and is only needed to author a rule, so it runs *after* the `rule="auto"` output misses the intent, not up front. It returns the metadata it fetched on `exp.meta` (so authoring needs no second `get_meta_info`), with `exp.roles` keyed by the same axis id the facts use. |
| **RuleV2 authoring defers to `docs/AUTHORING_RULES.md`** | That doc is the source of truth for the schema; the skill references it rather than restating it (information single-home). |
| Rules are saved to **`./pyestat_rules/*.yaml`** | The project-layer auto-discovery (task #15) is the receiver; a saved file applies with no code change and is git-versionable. |

## Data Flow

Intent + `statsDataId`
  → `get_meta_info(id)` members (axis id/name + codes) + a small `rule="auto"` sample (`to_flat`)
      → observe hazards (granularity mix, aggregate/detail, unexpected categories) → warn in plain language
      → this is the `select` vocabulary: keys = `class_obj.id`, values = member `code`
  → `get_stats_data(rule="auto")` → judge the output columns against the intent
  → branch on whether the `rule="auto"` output serves the intent:
      • serves the intent → narrow with `select` / `aggregates` / filter → deliver the data → **done**
      • falls short of the intent → `explain_table(id)` → role_pattern / coverage / proposed_rule
        + `exp.meta` (the facts, already in hand) → author a RuleV2 (from `proposed_rule`,
        or from the reported `role_pattern` when it is `None`)
          → save `./pyestat_rules/<name>.yaml` → re-run `rule="auto"` to verify → deliver → **done**

## Constraints & Tradeoffs

- **Table discovery is out of scope**: the skill assumes a `statsDataId` is known
  (offers only a pointer to `list_stats` / e-Stat search if it is missing).
- **Needs an appId and data access**: sampling (and `explain_table`, in the
  authoring branch) fetch real data (one page), so a valid `ESTAT_APP_ID` and a
  fetchable table are required; transient e-Stat outages are retried, not masked.
- **Goal integrity**: if a fetch fails, the skill reports the failure — it never
  fabricates structured data or a false "all clear".
- **Sampling is first-page only**: classification matches the auto path in the
  common case; a unit member appearing only in a later page is a rare residual
  divergence, accepted over always fetching whole tables.
- **Distribution is unresolved here**: the skill lives in the repo's
  `.claude/skills/`; packaging it for pip-installed pyestat users (a plugin, or a
  copyable resource) is a follow-up.
