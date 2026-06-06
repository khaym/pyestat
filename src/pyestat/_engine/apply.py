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
from pyestat._engine.classifier import AxisRole, TableClassification, _norm, classify
from pyestat._engine.role_defaults import TRANSFORMS, expand_short_form
from pyestat._engine.rule import OutputColumn, RuleV2
from pyestat._engine.time import best_effort
from pyestat.errors import RoleResolutionError, RuleExpansionError

# A ``where`` selector and a meta-axis member name must compare equal despite
# full/half-width drift; the pivot reuses the classifier's ``_norm`` (NFKC) so
# matching folds names exactly as the classifier folded them to detect the
# meta-axis in the first place — the two cannot drift apart.


def apply_rule(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
    stats_data_id: str,
    rule: "RuleV2 | Literal['heuristic'] | None",
    classification: TableClassification | None = None,
) -> tuple[dict[str, Any], ...]:
    """Run the requested transformation mode over ``values``.

    ``rule`` is already-resolved (the ``"auto"`` chain is collapsed by the
    caller — see :func:`apply_auto`):

    * ``None`` — raw mode; the flattened Layer 2 rows pass through.
    * ``"heuristic"`` — **Layer D** (#23): best-effort ``time``
      normalization plus additive labels, preserving raw data.
    * :class:`RuleV2` — a v2 output-schema rule applied against the
      ``classification`` (which axis plays which role).

    ``classification`` is the request-path classification (#28); the
    Layer D and v2 modes need it. When a standalone caller omits it, it is
    computed from the rows here so those modes stay self-contained.

    ``stats_data_id`` is retained for parity with the request path and
    future per-table diagnostics, though no current mode consults it.
    """
    if rule is None:
        return values
    if classification is None:
        classification = classify(class_objs, rows=values)
    if rule == "heuristic":
        return _apply_layer_d(values, class_objs, classification)
    if isinstance(rule, RuleV2):
        return apply_v2_rule(values, classification, rule, class_objs=class_objs)
    raise TypeError(
        f"rule must be RuleV2, 'heuristic', or None; got {type(rule).__name__}"
    )


def apply_auto(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
    classification: TableClassification,
    resolved: "RuleV2 | None",
) -> tuple[dict[str, Any], ...]:
    """Apply the ``rule="auto"`` decision, demoting v2 resolution failures
    to Layer D so the auto path never surfaces them.

    ``resolved`` is the resolver's output: a :class:`RuleV2` (Layer C / B /
    A) or ``None`` (route to Layer D). A resolved rule that cannot bind to
    this table — :class:`RoleResolutionError` (a role missing or ambiguous)
    or :class:`RuleExpansionError` (a short-form column that cannot expand)
    — falls back to Layer D rather than erroring, per the errors module
    contract. Other exceptions (e.g. a user transform raising) are *not*
    swallowed: role-defaults are total, so such a failure is a real bug.
    """
    if resolved is None:
        return _apply_layer_d(values, class_objs, classification)
    try:
        return apply_v2_rule(values, classification, resolved, class_objs=class_objs)
    except (RoleResolutionError, RuleExpansionError):
        return _apply_layer_d(values, class_objs, classification)


def _apply_layer_d(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
    classification: TableClassification,
) -> tuple[dict[str, Any], ...]:
    """Layer D — the no-rule fallback (#23): preserve data, normalize nothing
    structural.

    The axis ``classification`` (computed once on the request path, #28, not
    a hand-written rule) decides which axis is ``time``; that axis gets a
    best-effort normalization. Every axis with a CLASS table gains an
    additive ``{axis_id}_label``. Raw codes stay in place, the cell value is
    never coerced, and a code no parser recognises is left untouched —
    Layer D never raises and never drops a row. ``area`` is passed through;
    standard-code mapping is task #4's job.
    """
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


# --- v2 application (output-schema-first, #22) ------------------------------
#
# Application needs the *classification* (which axis plays which role) to
# resolve each column's ``source.role`` to an axis; the request-path wiring
# that runs the classifier and feeds it here is #28, so this function takes
# the classification as an argument and stays testable in isolation.
#
# Two shapes share this entry point. The default is 1:1 — one output row per
# input row — where a referenced role must resolve to exactly one axis. When a
# column's ``meta-axis`` source carries a ``where`` predicate, the rule pivots
# (#10): rows are folded by the non-meta axes into one record per group and
# each predicate selects a member's cell. ``class_objs`` carries the meta-axis
# member names the predicate matches against (by NFKC-normalized name).


def apply_v2_rule(
    values: tuple[dict[str, Any], ...],
    classification: TableClassification,
    rule: RuleV2,
    class_objs: Sequence[ClassObj] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Apply a v2 rule's output-column declarations to ``values``.

    Expands the rule defensively first, so a short-form rule (e.g. a
    Layer A rule #28 builds in memory) applies without a separate load
    step. A rule with any ``where``-predicate column pivots (N:1, #10);
    otherwise each input row maps to one output row (1:1).

    Raises :class:`RoleResolutionError` — a typed, catchable error — when a
    column's role is absent or ambiguous, when a pivot lacks the metadata or
    a single meta-axis it needs, so the auto path can fall back to Layer D
    rather than surface the failure.
    """
    rule = expand_short_form(rule)
    role_to_axes: dict[AxisRole, list[str]] = defaultdict(list)
    for axis in classification.axes:
        role_to_axes[axis.role].append(axis.axis_id)
    if any(col.source.where is not None for col in rule.output):
        return _apply_pivot(values, classification, rule, role_to_axes, class_objs)
    plan = [(col.column, _resolve_source(col, role_to_axes)) for col in rule.output]
    return tuple({column: read(row) for column, read in plan} for row in values)


def _apply_pivot(
    values: tuple[dict[str, Any], ...],
    classification: TableClassification,
    rule: RuleV2,
    role_to_axes: dict[AxisRole, list[str]],
    class_objs: Sequence[ClassObj] | None,
) -> tuple[dict[str, Any], ...]:
    """Fold meta-axis-spread rows into one record per non-meta group (#10).

    Groups ``values`` by the codes of every non-meta axis, then for each
    group emits one row: non-meta columns read the group's shared codes, and
    each ``where`` column selects the member whose (NFKC-normalized) name
    matches and surfaces its cell — ``None`` when that member is absent from
    the group, so a dropped measure (e.g. CPI's retired weight series) leaves
    a stable column rather than dropping the record.
    """
    meta_axes = role_to_axes.get(AxisRole.META_AXIS, [])
    if len(meta_axes) != 1:
        raise RoleResolutionError(
            role="meta-axis",
            reason=f"pivot needs exactly one meta-axis, found {meta_axes or 'none'}",
        )
    meta_id = meta_axes[0]
    if class_objs is None:
        raise RoleResolutionError(
            role="meta-axis",
            reason="pivot needs class metadata to match `where` by member name",
        )
    name_by_code = {
        c["code"]: _norm(str(c.get("name", c["code"])))
        for obj in class_objs
        if obj.id == meta_id
        for c in obj.classes
        if "code" in c
    }
    group_axis_ids = [
        a.axis_id for a in classification.axes if a.role != AxisRole.META_AXIS
    ]
    nonmeta_plan = [
        (col.column, _resolve_source(col, role_to_axes))
        for col in rule.output
        if col.source.where is None
    ]
    meta_plan = [
        (col.column, _norm(col.source.where.equals), TRANSFORMS.resolve(col.transform))
        for col in rule.output
        if col.source.where is not None
    ]

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in values:
        key = tuple(row.get(aid) for aid in group_axis_ids)
        groups.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for rows in groups.values():
        rep = rows[0]
        record = {column: read(rep) for column, read in nonmeta_plan}
        cell_by_name: dict[str, Any] = {}
        for row in rows:
            name = name_by_code.get(row.get(meta_id))
            if name is not None and name not in cell_by_name:
                cell_by_name[name] = row.get("value")
        for column, target, transform in meta_plan:
            raw = cell_by_name.get(target)
            record[column] = transform(raw) if raw is not None else None
        out.append(record)
    return tuple(out)


def _resolve_source(
    col: OutputColumn, role_to_axes: dict[AxisRole, list[str]]
) -> Callable[[dict[str, Any]], Any]:
    """Bind one output column to a per-row reader, resolved once per call.

    The VALUE role is special: its value is the observation cell
    (``value``), not an axis code — the classifier assigns VALUE to the
    single-member ``tab`` axis, but the number lives under ``value`` in
    Layer 2's flattened row.

    A bare ``meta-axis`` source (no ``where``) cannot bind to a single
    member, so it raises here; the pivot path handles ``where`` columns
    before reaching this function.
    """
    role = col.source.role
    if role == AxisRole.META_AXIS:
        raise RoleResolutionError(
            role=role.value,
            reason="a meta-axis output column needs a `where` predicate to select a member (#10)",
        )
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
                    "#10's where predicate disambiguates a meta-axis, not a "
                    "repeated non-meta role, which is not yet addressable"
                ),
            )
        key = axes[0]

    def read(row: dict[str, Any]) -> Any:
        raw = row.get(key)
        return transform(raw) if raw is not None else raw

    return read
