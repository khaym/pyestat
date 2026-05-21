"""Glues the Rule Engine (Layer 3) onto a fetched response (Layer 2).

Stays low-level on purpose — operates on the value tuple and the
class_objs list, never on ``StatsDataResponse`` — so the dependency
graph stays a clean DAG (the endpoint module is free to call into
here without needing this module to know its result type).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pyestat._endpoint import ClassObj
from pyestat._engine.rule import Rule
from pyestat._engine.transformers import TimeNormalizer, TransformContext, ValueCaster


def apply_rule(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
    stats_data_id: str,
    rule: "Rule | Literal['heuristic'] | None",
) -> tuple[dict[str, Any], ...]:
    """Run the requested transformation mode over ``values``.

    ``rule`` here is already-resolved: the ``"auto"`` fallback that
    callers may pass to :meth:`EstatClient.get_stats_data` is collapsed
    into either a concrete :class:`Rule` (when a built-in / project rule
    matched) or ``"heuristic"`` (when nothing matched) before reaching
    this function. Keeping ``apply_rule`` agnostic of that fallback
    means a future direct caller cannot get the resolution chain wrong.
    """
    if rule is None:
        return values
    if rule == "heuristic":
        return _apply_heuristic(values, class_objs)
    if isinstance(rule, Rule):
        return _apply_full(values, class_objs, stats_data_id, rule)
    raise TypeError(
        f"rule must be Rule, 'heuristic', or None; got {type(rule).__name__}"
    )


def _apply_heuristic(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
) -> tuple[dict[str, Any], ...]:
    """Heuristic mode: add ``{axis_id}_label`` for each axis with a CLASS table.

    Codes stay where they were so any downstream code/filter logic
    continues to work; the label is purely additive (Decision B's
    "safe defaults").
    """
    lookup: dict[str, dict[str, str]] = {
        obj.id: {c["code"]: c.get("name", c["code"]) for c in obj.classes if "code" in c}
        for obj in class_objs
    }
    return tuple(_label_row(row, lookup) for row in values)


def _label_row(row: dict[str, Any], lookup: dict[str, dict[str, str]]) -> dict[str, Any]:
    out = dict(row)
    for axis_id, codes_to_names in lookup.items():
        code = row.get(axis_id)
        if isinstance(code, str) and code in codes_to_names:
            out[f"{axis_id}_label"] = codes_to_names[code]
    return out


def _apply_full(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
    stats_data_id: str,
    rule: Rule,
) -> tuple[dict[str, Any], ...]:
    """Run the bundled Transformer pipeline against an explicit rule."""
    ctx = TransformContext(
        stats_data_id=stats_data_id,
        class_inf={obj.id: obj.classes for obj in class_objs},
        axes_meta={obj.id: obj.name for obj in class_objs},
    )
    rows: Any = iter(values)
    rows = TimeNormalizer().transform(rows, rule, ctx)
    rows = ValueCaster().transform(rows, rule, ctx)
    return tuple(rows)
