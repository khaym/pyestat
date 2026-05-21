# pyestat Use Cases

Captured: 2026-05-20. Documents the user-facing scenarios pyestat is
built to serve, so feature scoping can be measured against actual
needs rather than abstract API completeness.

## Primary (Originating)

**Personal financial scenario modeling and asset management
simulation.**

Concrete examples:

- *Inflation risk planning*: Pull 20–30 years of CPI history to feed
  long-run expense projections.
- *Wage trends*: Pull wage structure data by age bracket to inform
  income forecasting.
- *Household consumption*: Use the household consumption survey to
  estimate the cost of maintaining a given standard of living over
  decades.

This is the originating motivation. The barrier today is not a lack
of e-Stat data — it's that going from "I want to model inflation" to
"I have the right table parsed into a usable shape" requires deep
knowledge of e-Stat's catalog and wire format.

## Adjacent (Plausible Extension)

The same barrier blocks neighboring use cases:

- **Finance & investment**: Macro indicators (GDP, trade balance,
  population) as scenario inputs.
- **Real estate**: Population projections, housing starts, official
  land prices, broken down by region.
- **Research & journalism**: Building a time series for one theme by
  stitching across multiple tables.
- **LLM agents**: Natural-language query → table discovery →
  structured data fetch → grounded answer.

## Shared Barriers and pyestat's Approach

| Barrier | Approach (cross-reference) |
|---|---|
| User doesn't know which table to look at | `getStatsList` support (Decision G); future intent-based search helper |
| Table structure varies per table | Rule-driven transformation (DESIGN.md, Decisions A–E) |
| Code-value knowledge required (time/area/items) | Built-in rules + standard-code normalization (task #4) |
| Aggregate and detail codes intermixed | `exclude_aggregate` (planned rule extension, Decision D) |
| 100k-row pagination per response | Auto-fetch-all with `max_rows` warning (Decision I) |
| `time` representation varies by table | Built-in time parsers + granularity metadata (Decision D) |
| `value` type varies within one response | `value.type: conditional` (planned rule extension, Decision D) |

## Out of Scope (For Now)

- **Cross-granularity aggregation** (e.g. monthly → yearly).
  Delegated to the caller's analysis layer (pandas, polars, etc.).
  pyestat ships enough metadata (`time_granularity`) for the caller
  to perform the aggregation.
- **Analytics, forecasting, or visualization.** pyestat stops at
  delivering clean, structured data.
- **Statistical portals other than e-Stat.** No abstraction layer
  attempting to unify multiple government data sources.
