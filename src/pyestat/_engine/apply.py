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
from pyestat._engine.classifier import AxisRole, classify
from pyestat._engine.rule import Rule
from pyestat._engine.time import TimePoint, monthly_e_stat, quarterly_e_stat, yearly
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
    this function. ``"heuristic"`` runs **Layer D** (#23): best-effort
    ``time`` normalization plus additive labels, preserving raw data.
    Keeping ``apply_rule`` agnostic of that fallback means a future
    direct caller cannot get the resolution chain wrong.
    """
    if rule is None:
        return values
    if rule == "heuristic":
        return _apply_layer_d(values, class_objs)
    if isinstance(rule, Rule):
        return _apply_full(values, class_objs, stats_data_id, rule)
    raise TypeError(
        f"rule must be Rule, 'heuristic', or None; got {type(rule).__name__}"
    )


def _apply_layer_d(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
) -> tuple[dict[str, Any], ...]:
    """Layer D — the no-rule fallback (#23): preserve data, normalize nothing
    structural.

    The axis classifier (not a hand-written rule) decides which axis is
    ``time``; that axis gets a best-effort normalization. Every axis with a
    CLASS table gains an additive ``{axis_id}_label``. Raw codes stay in
    place, the cell value is never coerced, and a code no parser recognises
    is left untouched — Layer D never raises and never drops a row. ``area``
    is passed through; standard-code mapping is task #4's job.
    """
    classification = classify(class_objs)
    time_axes = tuple(
        a.axis_id for a in classification.axes if a.role == AxisRole.TIME
    )
    lookup: dict[str, dict[str, str]] = {
        obj.id: {c["code"]: c.get("name", c["code"]) for c in obj.classes if "code" in c}
        for obj in class_objs
    }
    return tuple(_layer_d_row(row, time_axes, lookup) for row in values)


def _layer_d_row(
    row: dict[str, Any],
    time_axes: Sequence[str],
    lookup: dict[str, dict[str, str]],
) -> dict[str, Any]:
    out = _label_row(row, lookup)
    for axis_id in time_axes:
        code = out.get(axis_id)
        if not isinstance(code, str):
            continue
        point = _best_effort_time(code)
        if point is None:
            continue
        out[axis_id] = point.normalized
        out[f"{axis_id}_code"] = code
        out["time_granularity"] = point.granularity
    return out


def _best_effort_time(code: str) -> TimePoint | None:
    """Try the built-in time parsers in turn; return the first match or None.

    Layer D has no rule naming the format, so it probes monthly → quarterly
    → yearly (specific to general). Each raises ``ValueError`` on a shape it
    does not own, so a code none of them recognise yields ``None`` and is
    left raw.
    """
    for parser in (monthly_e_stat, quarterly_e_stat, yearly):
        try:
            return parser(code)
        except ValueError:
            continue
    return None


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
