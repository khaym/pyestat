"""Layer 3: post-fetch transformation pipeline.

Owns the order — classify → aggregate-select → resolve → apply — and the
Layer A–D routing plus the surface-vs-degrade error policy, taking already-
fetched rows and class metadata rather than driving HTTP. Kept separate from
the endpoint so this routing business rule is testable from hand-built rows +
``class_objs``, with no transport to mock.

Stays low-level on purpose — operates on the value tuple and the ``class_objs``
list, never on ``StatsDataResponse`` — so the dependency graph stays a clean
DAG: the endpoint module (Layer 2) calls into here, never the other way at
module-import time.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pyestat._endpoint import ClassObj
from pyestat._engine.aggregate import select_rows
from pyestat._engine.apply import apply_auto, apply_rule
from pyestat._engine.classifier import classify
from pyestat._engine.resolver import resolve_v2
from pyestat._engine.rule import RuleV2


def run_pipeline(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
    table_inf: Mapping[str, Any] | None,
    stats_data_id: str,
    rule: "RuleV2 | Literal['auto', 'heuristic'] | None",
    aggregates: Literal["include", "exclude", "only"],
    *,
    user_rules: Sequence[RuleV2],
    project_rules: Sequence[RuleV2],
    builtin_rules: Sequence[RuleV2],
) -> tuple[dict[str, Any], ...]:
    """Transform already-fetched rows for one table, owning the Layer A–D
    routing and the surface-vs-degrade policy (``docs/DESIGN.md`` Decision B):

    * ``"auto"`` — classify, then resolve a rule through Layers C > B > A > D
      and apply it; a caller-authored rule that fails surfaces, a built-in or
      generic one degrades to Layer D.
    * ``"heuristic"`` — the Layer D fallback directly.
    * ``None`` — raw mode; Layer 2's flattened rows verbatim.
    * :class:`RuleV2` — apply this rule directly, bypassing resolution.

    ``aggregates`` filters detail / aggregate rows (#36) before any rule, so
    every mode honors it.

    Classifies once, on the *unfiltered* rows, so #27's data-driven meta-axis
    signal sees the whole table; the result feeds the aggregate selection,
    resolution, v2 application, and the Layer D fallback. Classification is
    computed only when ``"auto"`` needs it or an aggregate selection does — raw
    mode with the default never classifies.
    """
    classification = None
    if rule == "auto" or aggregates != "include":
        classification = classify(class_objs, table_inf, rows=values)

    if aggregates != "include":
        # Filter detail / aggregate rows before any rule runs, so every mode
        # honors the selection and a downstream pivot folds only the chosen
        # grain (#36).
        values = select_rows(values, classification, class_objs, aggregates)

    if rule == "auto":
        resolved = resolve_v2(
            classification,
            user=user_rules,
            project=project_rules,
            builtin=builtin_rules,
            class_objs=class_objs,
            stats_data_id=stats_data_id,
        )
        return apply_auto(values, class_objs, classification, resolved)
    # raw (``None``) and the explicit modes classify lazily inside
    # ``apply_rule`` only when needed; pass any classification already computed
    # for the aggregate selection so it is not recomputed on the post-filter
    # rows.
    return apply_rule(values, class_objs, stats_data_id, rule, classification)
