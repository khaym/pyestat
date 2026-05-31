# pyestat Use Cases

Last revised: 2026-05-31. For evaluators deciding whether pyestat fits
their workflow, and for maintainers scoping features against real
demand. Documents the user-facing scenarios pyestat is built to serve,
so feature scoping can be measured against actual needs rather than
abstract API completeness.

## Apex

pyestat lets LLMs and analysts fetch any e-Stat table as structured data
— without learning the catalog, the wire format, or e-Stat's
table-by-table quirks.

## Primary use cases

The same barrier — going from "I want data X" to "I have the right table
parsed into a usable shape" — blocks several scenarios. pyestat treats
all of them as primary:

- **LLM agents** (cross-cuts the domains below): Natural-language query
  → table discovery → structured data fetch → grounded answer. pyestat
  is the "structured fetch" layer the agent calls, regardless of the
  end domain.
- **Finance & investment**: Macro indicators (GDP, trade balance,
  population) as scenario inputs.
- **Real estate**: Population projections, housing starts, official land
  prices, broken down by region.
- **Research & journalism**: Building a time series for one theme by
  stitching across multiple tables.
- **Personal financial scenario modeling**: Long-run CPI / wage /
  household-consumption series for inflation, income, and lifestyle-cost
  modeling. (Originating use case — see [Originating context](#originating-context).)

## Shared barriers and pyestat's approach

| Barrier | Approach (cross-reference) |
|---|---|
| User doesn't know which table to look at | `getStatsList` support (Decision G); future intent-based search helper |
| Table structure varies per table | Rule-driven transformation (DESIGN.md, Decisions A–E) |
| Code-value knowledge required (time/area/items) | Built-in rules + standard-code normalization (task #4) |
| Aggregate and detail codes intermixed | `exclude_aggregate` (planned rule extension, Decision D) |
| 100k-row pagination per response | Auto-fetch-all with `max_rows` warning (Decision I) |
| `time` representation varies by table | Built-in time parsers + granularity metadata (Decision D) |
| `value` type varies within one response | `value.type: conditional` (planned rule extension, Decision D) |
| Table is not yet covered by a rule | Heuristic fallback: parse axes best-effort and return raw values; rules can be added later via Skill (#8) and project-local YAML (#15) |

## Originating context

pyestat began as a personal-finance modeling tool. The original use case
— long-run CPI / wage / household-consumption series for inflation,
income, and lifestyle-cost modeling — surfaced the structural problem
that turned out to apply far more broadly: e-Stat's value is real, but
the path from "I want data X" to "I have it in a usable shape" is steep.
The other primary use cases above share this barrier.

## Out of Scope (For Now)

- **Complete structuring of every e-Stat table.** Uncatalogued tables
  fall back to heuristic mode, which preserves data but may not
  normalize axes. Aim for catalog coverage of high-traffic tables and
  rely on Skill / project rules for the long tail.
- **Cross-granularity aggregation** (e.g. monthly → yearly).
  Delegated to the caller's analysis layer (pandas, polars, etc.).
  pyestat ships enough metadata (`time_granularity`) for the caller
  to perform the aggregation.
- **Analytics, forecasting, or visualization.** pyestat stops at
  delivering clean, structured data.
- **Statistical portals other than e-Stat.** No abstraction layer
  attempting to unify multiple government data sources.
