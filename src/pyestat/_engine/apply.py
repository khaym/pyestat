"""Glues the Rule Engine (Layer 3) onto a fetched response (Layer 2).

Stays low-level on purpose — operates on the value tuple and the
class_objs list, never on ``StatsDataResponse`` — so the dependency
graph stays a clean DAG (the endpoint module is free to call into
here without needing this module to know its result type).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any, Literal

from pyestat._endpoint import ClassObj
from pyestat._engine.classifier import AxisRole, TableClassification, classify
from pyestat._engine.role_defaults import TRANSFORMS, expand_short_form
from pyestat._engine.rule import OutputColumn, Rule, RuleV2
from pyestat._engine.time import best_effort
from pyestat._engine.transformers import TimeNormalizer, TransformContext, ValueCaster
from pyestat.errors import RoleResolutionError


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
        point = best_effort(code)
        if point is None:
            continue
        out[axis_id] = point.normalized
        out[f"{axis_id}_code"] = code
        out["time_granularity"] = point.granularity
    return out


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


# --- v2 application (output-schema-first, #22) ------------------------------
#
# The v2 counterpart of ``_apply_full``. It needs the *classification* (which
# axis plays which role) to resolve each column's ``source.role`` to an axis;
# the request-path wiring that runs the classifier and feeds it here is #28,
# so this function takes the classification as an argument and stays testable
# in isolation. ``where``-predicate pivot (a role spanning several axes) is
# #10; until then a referenced role must resolve to exactly one axis.


def apply_v2_rule(
    values: tuple[dict[str, Any], ...],
    classification: TableClassification,
    rule: RuleV2,
) -> tuple[dict[str, Any], ...]:
    """Apply a v2 rule's output-column declarations to ``values``.

    Expands the rule defensively first, so a short-form rule (e.g. a
    Layer A rule #28 builds in memory) applies without a separate load
    step. Raises :class:`RoleResolutionError` — a typed, catchable error
    — when a column's role is absent or ambiguous, so the auto path can
    fall back to Layer D rather than surface the failure.
    """
    rule = expand_short_form(rule)
    role_to_axes: dict[AxisRole, list[str]] = defaultdict(list)
    for axis in classification.axes:
        role_to_axes[axis.role].append(axis.axis_id)
    plan = [(col.column, _resolve_source(col, role_to_axes)) for col in rule.output]
    return tuple({column: read(row) for column, read in plan} for row in values)


def _resolve_source(
    col: OutputColumn, role_to_axes: dict[AxisRole, list[str]]
) -> Callable[[dict[str, Any]], Any]:
    """Bind one output column to a per-row reader, resolved once per call.

    The VALUE role is special: its value is the observation cell
    (``value``), not an axis code — the classifier assigns VALUE to the
    single-member ``tab`` axis, but the number lives under ``value`` in
    Layer 2's flattened row.
    """
    role = col.source.role
    transform = TRANSFORMS.resolve(col.transform)
    if role == AxisRole.VALUE:
        key = "value"
    else:
        axes = role_to_axes.get(role, [])
        if not axes:
            raise RoleResolutionError(
                role=role.value,
                reason=f"no axis is classified as {role.value} in this table",
            )
        if len(axes) > 1:
            raise RoleResolutionError(
                role=role.value,
                reason=(
                    f"multiple axes are classified as {role.value} ({axes}); "
                    "disambiguating needs a where-predicate pivot (#10)"
                ),
            )
        key = axes[0]

    def read(row: dict[str, Any]) -> Any:
        raw = row.get(key)
        return transform(raw) if raw is not None else raw

    return read
