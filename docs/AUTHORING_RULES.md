# Writing your own rules

Write a `RuleV2` to structure an e-Stat table `pyestat` ships no built-in rule
for. You declare the **output columns** you want; each draws on an axis *role*
the classifier infers, so one rule covers every table sharing that role pattern.

This is the *authoring* surface. Unlike the consumption side — the nested
`StatsDataResponse` and its `to_flat()` projection, which hold across 0.x — the
`RuleV2` schema is **evolving**: it may change across 0.x as built-in coverage
grows (see the [Status](../README.md#status) section).

## How a rule matches

`pyestat` classifies a table's axes into roles — `time`, `area`, `category`,
`value`, `meta-axis`, and others — and forms an ordered `role_pattern`. A rule's
`match.role_pattern` is compared against it; axis ids never appear, so the same
rule fires on every table with that pattern. A user rule that matches a table's
role pattern shadows a built-in for the same pattern.

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

`match.stats_code` is an optional extra narrowing: set it to an e-Stat
`statsCode` and the rule fires only on that survey family. Use it when a rule's
selectors are tied to one survey's member names, so a structurally identical
table from another family declines the rule rather than folding into empty rows.
Omitted, the rule matches by role pattern alone.

## Discovering a table's role pattern

You don't have to guess the pattern. `explain_table` reports how `pyestat`
classifies a table — the ordered `role_pattern` your `match` must equal, each
axis's role and confidence, which layer would cover it, and a proposed rule to
start from:

```python
exp = client.explain_table("0004049327")
exp.role_pattern    # ('category', 'meta-axis', 'area', 'time')
exp.coverage        # 'builtin' | 'user' | 'project' | 'generic' | 'fallback'
exp.proposed_rule   # a RuleV2 to hand-edit, or None when none can be generated
for a in exp.axes:
    print(a.axis_id, a.role, a.confidence, a.signals)
```

`coverage` tells you whether authoring is even needed: `builtin` / `user` /
`project` means a specific rule already fires; `generic` means the auto path
structures it from roles alone (edit `proposed_rule` for different columns);
`fallback` means the table is too low-confidence or unstructurable and rides the
lossless Layer D until a rule covers it.

It classifies from a sample of the table's data (its first page) — the same
data-driven view `rule="auto"` uses. Metadata alone cannot reliably tell a
measure-spread `meta-axis` from a plain `category` (an axis merely *named* like
a measure, 数量 / 金額 / …, would misclassify), so the `role_pattern` it reports
is the one the auto path actually matches against, not a metadata guess.

`explain_table` interprets *structure*, not data content: a time axis mixing
calendar and fiscal years, or aggregate rows intermixed with detail, are
member-level facts it does not flag — inspect the raw members via
`get_meta_info` (and `aggregates=` / `select`) for those.

## Columns: short and long form

An output column has three fields: `column` (the output name), `source` (the
role it draws from), and `transform` (how the cell is rendered). Long form sets
all three. Short form omits what the role-default registry can fill:

- `{"column": "time"}` — the name doubles as the role; source and transform
  default.
- `{"column": "year", "source": {"role": "time"}}` — explicit source, default
  transform.

A role fixes the cell shape regardless of form: a `time` cell, a `{code, label}`
dimension, a `{value, unit}` measure.

### Selecting a specific axis

By default a role resolves to the one axis that carries it — the common case
(one `time`, one `area`). When a table has *two* axes of the same role —
建築主 × 用途, 職種 × 企業規模 — name each with `source.axis` (the axis id) to
map them to separate columns; role addressing alone cannot tell them apart.
`axis` is valid only on a directly-addressable role (`time` / `area` /
`category`).

## Transforms

Each role carries a default transform; pass `transform` to override it. For
example, a `time` column defaults to a normalized form, `yearly` projects it to
the year, and `passthrough` keeps the raw code unchanged.

## Folding spread rows into one record (pivot)

Some tables split one logical record across several rows — a `meta-axis` the
classifier flags, such as foreign trade's quantity / amount / unit. A `where`
predicate on a `meta-axis` source folds those rows by the remaining axes and
selects each measure into its own column, matching on the member **name** (not
its opaque code):

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

A measure absent from a group (e.g. a series retired in a base-year revision)
yields `None` for that column rather than dropping the record, so the output
shape stays stable across table versions. Conversely, a `where` predicate is a
projection: meta-axis members you do not select (e.g. a table's monthly
breakdowns when you keep only the annual totals) are dropped from the output —
declare a column for every member you need.

A `where` predicate takes at least one of three selectors, combined as AND, all
read from the member's metadata rather than its code:

- `equals` — the member's own name.
- `parent` — its parent member's name (selects a whole family at once).
- `level` — the member's `@level` depth, as a string.

Names are NFKC-normalized at apply time, so write the semantic label
(`"合計_金額"`) regardless of width drift.

## Naming columns for `to_flat()`

`to_flat()` derives suffix columns from each cell — `{col}_label`, a time
column's `{col}_code` / `{col}_granularity`, and the bare `unit` that a column
named exactly `value` carries. Pick output names so none equals another's
derived key: a `unit` column alongside a `value` measure, or a `region_label`
column alongside a dimension `region`, collide in the flat shape. The nested
form is never affected, so `response.values` always works; only `to_flat()` is
constrained. For your own rule a collision raises a typed error naming the
column to rename; a built-in that would collide degrades to raw output instead,
so it never lands on you. (A `unit` column is fine when no `value` measure
shares the row, as in the pivot above.)

## Loading rules from files

You can drop rule files in a directory instead of building them in Python:
`EstatClient` discovers `./pyestat_rules/*.yaml` (and `.yml`) by file placement
alone — no registration call. Each file is the same schema as the `RuleV2`
above, written as YAML.

- Relocate the directory with `project_rules_dir=`, or opt out with `None` /
  `""`.
- An invalid rule file raises a typed `EstatError` at construction, so a typo
  surfaces immediately rather than at query time.

## Advanced: folding a measure × period cross

When a single `meta-axis` crosses a measure with a period that lives only in the
member name — months × measure families, where `"1月_金額"` encodes both — two
modifiers on a `meta-axis` source fold the cross without enumerating every
member. A column carries `where` *or* `key`, never both:

- `key` — `{"pattern": "<regex>"}` derives a **grain dimension** from the member
  name; its first capture group becomes a row dimension the cross folds around.
- `unit_from` — a `where`-style predicate that fills a measure's `unit` from a
  grain-less unit member (trade ships a quantity's unit as a level-1 `単位2`
  member). It co-occurs with `where` on the same column.
