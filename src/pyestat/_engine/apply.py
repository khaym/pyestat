"""Glues the Rule Engine (Layer 3) onto a fetched response (Layer 2).

Stays low-level on purpose — operates on the value tuple and the
class_objs list, never on ``StatsDataResponse`` — so the dependency
graph stays a clean DAG (the endpoint module is free to call into
here without needing this module to know its result type).
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pyestat._endpoint import ClassObj
from pyestat._engine.canonical import dimension, measure, time_cell
from pyestat._engine.classifier import (
    AxisRole,
    TableClassification,
    _norm,
    classify,
    pivot_member_name,
)
from pyestat._engine.registry import RegistryKeyError
from pyestat._engine.resolver import ResolvedRule
from pyestat._engine.role_defaults import TIME_PARSERS, TRANSFORMS, expand_short_form
from pyestat._engine.rule import MetaWhere, OutputColumn, RuleV2
from pyestat._errors import (
    RoleResolutionError,
    RuleAuthoringError,
    TimeFormatError,
    UnknownTransformError,
)

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
    * ``"heuristic"`` — **Layer D**: best-effort ``time``
      normalization plus additive labels, preserving raw data.
    * :class:`RuleV2` — a v2 output-schema rule applied against the
      ``classification`` (which axis plays which role).

    ``classification`` is the request-path classification; the
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
    resolved: "ResolvedRule | None",
) -> tuple[dict[str, Any], ...]:
    """Apply the ``rule="auto"`` decision, surfacing or degrading an
    application failure by the resolved rule's provenance (ARCHITECTURE.md).

    ``resolved`` is the resolver's output: a :class:`ResolvedRule` (the rule
    paired with the layer it came from) or ``None`` (route to Layer D). When
    applying the rule fails with a :class:`RuleAuthoringError` (a role
    missing or ambiguous, a short-form column that cannot expand, or a
    transform name the registry lacks), a caller-authored layer re-raises
    and a library-provided one degrades to Layer D. Catching the shared base
    routes any future authoring-error leaf by the same policy without editing
    this clause.

    Other exceptions are *not* caught: a registered transform raising at
    runtime is a real bug, not an authoring error, and surfaces regardless
    of layer (role-defaults are total, so a Layer A generic rule cannot
    reach here).
    """
    if resolved is None:
        return _apply_layer_d(values, class_objs, classification)
    try:
        return apply_v2_rule(
            values, classification, resolved.rule, class_objs=class_objs
        )
    except RuleAuthoringError:
        if resolved.layer.is_caller_authored:
            raise
        return _apply_layer_d(values, class_objs, classification)


def _apply_layer_d(
    values: tuple[dict[str, Any], ...],
    class_objs: Sequence[ClassObj],
    classification: TableClassification,
) -> tuple[dict[str, Any], ...]:
    """Layer D — the no-rule fallback: preserve data, normalize nothing
    structural.

    The axis ``classification`` (computed once on the request path, not
    a hand-written rule) decides which axis is ``time``. Output is the
    canonical nested form: every classified axis becomes a
    ``{code, label}`` cell — the ``time`` axis a full :func:`time_cell` — and
    the observation becomes a ``{value, unit}`` measure. Raw codes stay in
    each cell's ``code``, the value is never coerced, and a code no parser
    recognises keeps ``normalized == code`` — Layer D never raises and never
    drops a row. ``area`` is passed through as a plain dimension;
    standard-code mapping is out of scope here.
    """
    time_axes = frozenset(
        a.axis_id for a in classification.axes if a.role == AxisRole.TIME
    )
    lookup = _label_lookup(class_objs)
    return tuple(_layer_d_row(row, time_axes, lookup) for row in values)


def _label_lookup(
    class_objs: Sequence[ClassObj] | None,
) -> dict[str, dict[str, str]]:
    """Per axis id, a ``code -> display name`` map for building ``label``s.

    Shared by Layer D and the v2 1:1/pivot paths so both resolve a code's
    human label the same way. A label-less axis (no ``CLASS`` entries) maps
    to an empty dict, and callers fall back to the code itself — the canonical
    ``{code, label}`` cell is then ``label == code`` rather than partial.
    """
    if class_objs is None:
        return {}
    return {
        obj.id: {c["code"]: c.get("name", c["code"]) for c in obj.classes if "code" in c}
        for obj in class_objs
    }


def _layer_d_row(
    row: dict[str, Any],
    time_axes: frozenset[str],
    lookup: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Rebuild one row as canonical cells.

    A label-less code carries its own code as the label, so a dimension cell
    is never partial. ``unit`` is folded into the ``value`` measure rather
    than left as a sibling key.
    """
    out: dict[str, Any] = {}
    for key, raw in row.items():
        if key == "unit":
            continue  # absorbed into the value measure below
        if key == "value":
            out["value"] = measure(raw, row.get("unit"))
        elif key in lookup:
            label = _label(lookup[key], raw)
            # Layer D is the no-rule path: time is always best-effort normalized
            # (time_cell's default), never a declared strict format.
            out[key] = time_cell(raw, label) if key in time_axes else dimension(raw, label)
        else:
            out[key] = raw
    return out


def _label(codes: dict[str, str], code: Any) -> Any:
    """A code's display label from the class lookup, falling back to the code
    itself so a ``{code, label}`` cell is never partial — a label-less axis
    (trade HS codes, where ``code == name``) carries its code as the label."""
    return codes.get(code, code) if isinstance(code, str) else code


# --- v2 application (output-schema-first) ------------------------------
#
# Application needs the *classification* (which axis plays which role) to
# resolve each column's ``source.role`` to an axis; the request-path wiring
# that runs the classifier and feeds it here is the pipeline, so this function takes
# the classification as an argument and stays testable in isolation.
#
# Two shapes share this entry point. The default is 1:1 — one output row per
# input row — where a referenced role must resolve to exactly one axis. When a
# column's ``meta-axis`` source carries a ``where`` predicate, the rule pivots:
# rows are folded by the non-meta axes into one record per group and
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
    Layer A rule the resolver builds in memory) applies without a separate load
    step. A rule with any ``where``-predicate column pivots (N:1);
    otherwise each input row maps to one output row (1:1).

    Raises a :class:`RuleAuthoringError` when the rule cannot be applied as
    authored: :class:`RoleResolutionError` when a column's role is absent or
    ambiguous or a pivot lacks the metadata or single meta-axis it needs,
    :class:`UnknownTransformError` for an unknown transform name, or
    :class:`RuleExpansionError` for a short-form column that cannot expand.
    The auto path routes these by provenance (surface vs. Layer D — see
    :func:`apply_auto`); the explicit-rule path surfaces them to the caller.
    """
    rule = expand_short_form(rule)
    role_to_axes: dict[AxisRole, list[str]] = defaultdict(list)
    for axis in classification.axes:
        role_to_axes[axis.role].append(axis.axis_id)
    lookup = _label_lookup(class_objs)
    if any(
        col.source.where is not None or col.source.key is not None
        for col in rule.output
    ):
        return _apply_pivot(values, classification, rule, role_to_axes, class_objs, lookup)
    plan = [
        (col.column, _resolve_source(_bind_axis(col, role_to_axes), lookup))
        for col in rule.output
    ]
    return tuple({column: read(row) for column, read in plan} for row in values)


def _apply_pivot(
    values: tuple[dict[str, Any], ...],
    classification: TableClassification,
    rule: RuleV2,
    role_to_axes: dict[AxisRole, list[str]],
    class_objs: Sequence[ClassObj] | None,
    lookup: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], ...]:
    """Fold meta-axis-spread rows into one record per non-meta group, plus any
    grain a ``key`` derives.

    Groups ``values`` by the codes of every non-meta axis. A ``key`` column
    lifts a value out of each member's *name* (the trade cross encodes
    the month only there, e.g. ``"1月_金額"``) and adds it to the grain, so one
    group can emit several rows — one per derived key. Within each
    (group, grain) record:

    * non-meta columns read the group's shared codes as canonical cells;
    * a ``key`` column emits its derived value;
    * each ``where`` column **filters** the members of that record to the one
      matching its predicate (name / parent name / level — AND) and surfaces
      that member's cell as a ``{value, unit}`` measure, carrying *that
      member's own* unit so two measures with different units (trade's 数量 in
      ＮＯ vs 金額 in 千円) stay correct.

    A ``where`` matching no member yields ``None`` (not a measure), so a
    dropped measure (CPI's retired weight series) leaves a stable column rather
    than dropping the record. A ``where`` matching *several* members in one
    record is an authoring error (:class:`RoleResolutionError`): there is no
    single cell to surface, and the fix — adding a ``key`` to split them into
    rows — is the author's, so the auto path routes it to Layer D.
    """
    plan = _plan_pivot(rule, classification, role_to_axes, class_objs, lookup)
    out: list[dict[str, Any]] = []
    for rows in _group_rows(values, plan.group_axis_ids).values():
        base = {column: read(rows[0]) for column, read in plan.nonmeta_plan}
        for grain, candidates in _subgroup_by_grain(rows, plan):
            out.append(_build_record(base, grain, candidates, rows, plan))
    return tuple(out)


@dataclass(frozen=True)
class _PivotPlan:
    """Everything a pivot resolves once per call and threads to its per-group /
    per-record steps: the single meta-axis id and its member index, the non-meta
    grouping axes, and the three output-column plans (non-meta readers, ``key``
    patterns, ``where`` measures). Grouping these keeps the step signatures
    (:func:`_subgroup_by_grain`, :func:`_build_record`) about the *rows* they
    fold, not the plan they fold against — and a new per-column plan dimension
    is added here, not threaded through every signature.

    ``where_plan`` tuples are ``(column, predicate, unit_predicate, transform,
    on_ambiguous)``: the member selector, the optional ``unit_from`` selector,
    the measure transform, and the error a multi-member match raises.
    """

    meta_id: str
    index: _MemberIndex
    group_axis_ids: list[str]
    nonmeta_plan: list[tuple[str, Callable[[dict[str, Any]], Any]]]
    key_plan: list[tuple[str, "re.Pattern[str]"]]
    where_plan: list[
        tuple[
            str,
            Callable[[Any], bool],
            Callable[[Any], bool] | None,
            Callable[[Any], Any],
            Callable[[int], RoleResolutionError],
        ]
    ]


def _plan_pivot(
    rule: RuleV2,
    classification: TableClassification,
    role_to_axes: dict[AxisRole, list[str]],
    class_objs: Sequence[ClassObj] | None,
    lookup: dict[str, dict[str, str]],
) -> _PivotPlan:
    """Resolve everything a pivot needs once, before folding any rows: the
    single meta-axis and its member index, the non-meta grouping axes, and the
    non-meta / key / where output-column plans.

    The typed :class:`RoleResolutionError`\\ s a malformed pivot rule earns (no
    single meta-axis, missing metadata, an unaddressable non-meta role) fire
    here at plan time, before row iteration — so the auto path routes them to
    Layer D without having read a row.
    """
    meta_id, members = _resolve_meta_axis(role_to_axes, class_objs)
    index = _build_member_index(members)
    group_axis_ids = [
        a.axis_id for a in classification.axes if a.role != AxisRole.META_AXIS
    ]
    nonmeta_plan = [
        (col.column, _resolve_source(_bind_axis(col, role_to_axes), lookup))
        for col in rule.output
        if col.source.where is None and col.source.key is None
    ]
    key_plan = [
        (col.column, re.compile(col.source.key.pattern))
        for col in rule.output
        if col.source.key is not None
    ]
    where_plan = [
        (
            col.column,
            _member_predicate(col.source.where, index),
            (
                _member_predicate(col.source.unit_from, index)
                if col.source.unit_from is not None
                else None
            ),
            _resolve_transform(col.column, col.transform),
            _where_ambiguity(col.column),
        )
        for col in rule.output
        if col.source.where is not None
    ]
    return _PivotPlan(
        meta_id, index, group_axis_ids, nonmeta_plan, key_plan, where_plan
    )


def _group_rows(
    values: tuple[dict[str, Any], ...],
    group_axis_ids: Sequence[str],
) -> dict[tuple, list[dict[str, Any]]]:
    """Group rows by the codes of every non-meta axis — the records a pivot
    folds into one row each (before any ``key`` splits a group by grain)."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in values:
        key = tuple(row.get(aid) for aid in group_axis_ids)
        groups.setdefault(key, []).append(row)
    return groups


def _grain_of(row: dict[str, Any], plan: _PivotPlan) -> tuple:
    """The derived-key tuple for a row, reading each `key` pattern against the
    member's name. A pattern that does not match yields ``None`` for that
    component — the member sits outside the derived grain (a level-1 total under
    a month key) and forms no row of its own.

    The grain value is the first capture group, or the whole match when the
    pattern declares none. A pattern *with* a group that did not participate (an
    alternation/optional branch) falls back to the whole match too, so a member
    whose name the pattern *did* match is never silently dropped for an empty
    group.
    """
    name = plan.index.name_by_code.get(row.get(plan.meta_id), "")
    keys = []
    for _, pattern in plan.key_plan:
        match = pattern.search(name)
        if match is None:
            keys.append(None)
            continue
        captured = match.group(1) if pattern.groups else match.group(0)
        keys.append(captured if captured is not None else match.group(0))
    return tuple(keys)


def _subgroup_by_grain(
    rows: list[dict[str, Any]], plan: _PivotPlan
) -> list[tuple[tuple, list[dict[str, Any]]]]:
    """Sub-group a non-meta group's rows by the grain a ``key`` derives,
    dropping members outside the grain (a level-1 total under a month key).
    Without a ``key`` the whole group is one grain-less record."""
    if not plan.key_plan:
        return [((), list(rows))]
    by_grain: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        grain = _grain_of(row, plan)
        if any(value is None for value in grain):
            continue  # member outside the derived grain (e.g. a total)
        by_grain.setdefault(grain, []).append(row)
    return list(by_grain.items())


def _build_record(
    base: dict[str, Any],
    grain: tuple,
    candidates: list[dict[str, Any]],
    group_rows: list[dict[str, Any]],
    plan: _PivotPlan,
) -> dict[str, Any]:
    """Assemble one (group, grain) output record: the shared non-meta cells
    (``base``), the derived-key columns, and each ``where`` measure.

    A ``where`` selects its single member within ``candidates`` (the period
    grain); ``unit_from`` reads the unit across ``group_rows`` (the whole
    group), because the unit member (単位2) carries no period grain and so lives
    outside ``candidates``. A ``where`` matching nothing leaves a stable ``None``
    column (CPI's retired weight series) rather than dropping the record;
    matching several distinct members is the author's ambiguity to split with a
    ``key`` (routed to Layer D on the auto path).
    """
    record = dict(base)
    for (column, _), value in zip(plan.key_plan, grain):
        record[column] = value
    for column, predicate, unit_predicate, transform, on_ambiguous in plan.where_plan:
        row = _select_one_member(predicate, candidates, plan.meta_id, on_ambiguous)
        if row is None:
            record[column] = None
        else:
            unit = (
                _broadcast_unit(column, unit_predicate, group_rows, plan.meta_id)
                if unit_predicate is not None
                else row.get("unit")
            )
            record[column] = measure(_apply_transform(transform, row.get("value")), unit)
    return record


def _select_one_member(
    predicate: Callable[[Any], bool],
    pool: Sequence[dict[str, Any]],
    meta_id: str,
    on_ambiguous: Callable[[int], RoleResolutionError],
) -> dict[str, Any] | None:
    """The one meta member ``predicate`` selects within ``pool``, or ``None``
    when it matches nothing.

    Counts *distinct* members, not rows: a member duplicated within one pool (a
    malformed response, or an axis outside the role pattern) collapses to its
    first row — the earlier graceful behavior. Several *different* members
    matching one predicate **coalesce when they surface the same ``(value,
    unit)``**: 賃金構造 "DB" tables dual-code one measure under two member
    codes for a code-scheme vintage, so the overlap year carries identical
    values under both — one observation, not a conflict. Only members that
    genuinely *disagree* are an ambiguity the caller must narrow, raised via
    ``on_ambiguous`` (the guidance differs by caller: a ``where`` adds a ``key``
    to split rows, a ``unit_from`` narrows its selector).

    ``pool`` is the caller's to choose: a ``where`` selects within the period
    grain (``candidates``), a ``unit_from`` across the whole non-meta group
    (``rows``), because the unit member carries no grain.
    """
    matched = [r for r in pool if predicate(r.get(meta_id))]
    distinct = {r.get(meta_id) for r in matched}
    if len(distinct) > 1:
        surfaced = {(r.get("value"), r.get("unit")) for r in matched}
        if len(surfaced) > 1:
            raise on_ambiguous(len(distinct))
    return matched[0] if matched else None


def _where_ambiguity(column: str) -> Callable[[int], RoleResolutionError]:
    """The error a ``where`` matching several distinct members raises. Built
    once per column (with the plan), not per record, since it never fires on
    the happy path: the fix — adding a ``key`` to split the members into rows —
    is the author's, so the auto path routes it to Layer D."""
    return lambda n: RoleResolutionError(
        role="meta-axis",
        reason=(
            f"the `where` for column {column!r} matched "
            f"{n} distinct members in one record; a "
            "where selects a single member — add a `key` to "
            "split them into rows"
        ),
    )


def _broadcast_unit(
    column: str,
    predicate: Callable[[Any], bool],
    pool: Sequence[dict[str, Any]],
    meta_id: str,
) -> Any:
    """The unit string a ``unit_from`` broadcasts into a measure: the
    *value* of the one grain-less member its predicate selects across the
    group.

    Trade ships a quantity's unit as a level-1 member (``単位2``) whose
    observation value *is* the unit (``"ＮＯ"``), not an ``@unit`` attribute —
    so the unit is read from ``value``. ``pool`` is the whole non-meta group
    (not the period grain), because that member sits outside the grain. A
    predicate matching no member yields ``None`` — the same graceful stance a
    ``where`` matching nothing takes — while several *distinct* members that
    disagree on their value is an ambiguity the author must narrow (mirrors the
    ``where`` multi-match error; distinct members sharing one value coalesce).
    """
    row = _select_one_member(
        predicate,
        pool,
        meta_id,
        lambda n: RoleResolutionError(
            role="meta-axis",
            reason=(
                f"the `unit_from` for column {column!r} matched {n} "
                "distinct members; a unit_from selects a single unit member — "
                "narrow it (equals / parent / level)"
            ),
        ),
    )
    return row.get("value") if row is not None else None


@dataclass(frozen=True)
class _MemberIndex:
    """The three member→signal maps a ``where``/``key`` reads, keyed by member
    code.

    All names are NFKC-folded (via ``pivot_member_name``) so a selector and the
    member it targets compare equal — the same fold the classifier used to
    detect the meta-axis, so the two cannot drift. ``parent`` resolves to the
    parent member's *name* (the vocabulary ``where: {parent}`` speaks), so a
    measure family is selectable without naming each child; ``level`` is the raw
    ``@level`` string.
    """

    name_by_code: dict[str, str]
    parent_name_by_code: dict[str, str | None]
    level_by_code: dict[str, str]


def _resolve_meta_axis(
    role_to_axes: dict[AxisRole, list[str]],
    class_objs: Sequence[ClassObj] | None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """The single meta-axis id and its members-by-code for a pivot, or a typed
    :class:`RoleResolutionError`.

    A pivot folds exactly one meta-axis; zero or several is not
    pivotable here, and ``where``/``key`` need class metadata to match members
    by name.
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
    members = {
        c["code"]: c
        for obj in class_objs
        if obj.id == meta_id
        for c in obj.classes
        if "code" in c
    }
    return meta_id, members


def _build_member_index(members: dict[str, dict[str, Any]]) -> _MemberIndex:
    """Project the meta-axis members into the three signal maps a predicate
    reads (see :class:`_MemberIndex`)."""
    name_by_code = {code: pivot_member_name(c) for code, c in members.items()}
    parent_name_by_code = {
        code: (name_by_code.get(c["parentCode"]) if c.get("parentCode") else None)
        for code, c in members.items()
    }
    level_by_code = {code: str(c.get("level", "")) for code, c in members.items()}
    return _MemberIndex(name_by_code, parent_name_by_code, level_by_code)


def _member_predicate(
    where: "MetaWhere",
    index: "_MemberIndex",
) -> Callable[[Any], bool]:
    """Compile a ``where`` into a predicate over a meta member's code: ``True``
    when the member satisfies *every* selector given (AND).

    Names compare NFKC-folded (the index already applied the fold the classifier
    used, so a selector and the member it targets cannot drift); ``level``
    compares as the raw ``@level`` string. Each selector binds its target at
    definition so the closures do not share one loop variable.
    """
    checks: list[Callable[[Any], bool]] = []
    if where.equals is not None:
        checks.append(
            lambda code, target=_norm(where.equals): index.name_by_code.get(code)
            == target
        )
    if where.parent is not None:
        checks.append(
            lambda code, target=_norm(where.parent): index.parent_name_by_code.get(code)
            == target
        )
    if where.level is not None:
        checks.append(
            lambda code, target=where.level: index.level_by_code.get(code) == target
        )
    return lambda code: all(check(code) for check in checks)


def _resolve_transform(column: str, name: str) -> Callable[[Any], Any]:
    """Resolve a transform name, converting the registry's ``KeyError`` into
    a typed :class:`UnknownTransformError` that names the column.

    The registry raises a ``KeyError`` subclass; left bare it would escape
    the auto path's typed-error handling and crash the caller. Wrapping it
    here — the one place a rule's transform name meets the registry — keeps
    the contract that an unknown transform is a typed, provenance-routed
    authoring error (see ARCHITECTURE.md), not a stray ``KeyError``.
    """
    try:
        return TRANSFORMS.resolve(name)
    except RegistryKeyError as exc:
        raise UnknownTransformError(
            column=column, transform=name, known=TRANSFORMS.names()
        ) from exc


def _validate_transform(column: str, name: str) -> None:
    """Assert a transform name is known (typo → :class:`UnknownTransformError`)
    *without* binding the callable — for a column whose canonical cell is
    built structurally and never runs the scalar transform (a dimension's raw
    code; a time cell, whose parser comes from :data:`TIME_PARSERS`). Spelling
    this out as a name-check, not an unused ``_resolve_transform`` binding,
    keeps it clear the transform is validated but deliberately not applied."""
    _resolve_transform(column, name)


def _apply_transform(transform: Callable[[Any], Any], raw: Any) -> Any:
    """Run a measure's transform, passing ``None`` through untouched.

    Shared by the 1:1 VALUE reader and the pivot's ``where`` measures so both
    agree on what counts as 'no value' — change the missing-cell rule once."""
    return transform(raw) if raw is not None else raw


@dataclass(frozen=True)
class AxisBinding:
    """A non-meta output column resolved to a concrete axis and its cell shape,
    computed once before row iteration so the per-row reader never re-decides
    role-vs-id.

    ``axis_id`` is ``None`` exactly for VALUE, which reads the observation cell
    rather than an axis. META_AXIS never produces a binding — it flows through
    the pivot's ``where``/``key`` path, not :func:`_bind_axis`.
    """

    column: str
    role: AxisRole
    axis_id: str | None
    transform: str


def _bind_axis(
    col: OutputColumn, role_to_axes: dict[AxisRole, list[str]]
) -> AxisBinding:
    """Resolve one output column's ``source`` to an :class:`AxisBinding` up
    front, owning the addressing rule the engine had split across role- and
    id-based paths.

    A column names an axis id, or — naming none — falls back to the role's
    single axis (:func:`_single_axis`, which rejects a role that is absent or
    repeated). The named axis must actually carry the column's role; addressing
    one that does not (a typo, or a role the table lacks) is a typed
    :class:`RoleResolutionError` naming the axis, so the auto path routes to
    Layer D rather than reading every row's missing cell as ``None``.

    VALUE binds no axis — it reads the observation cell. A bare META_AXIS source
    cannot bind to a single member, so it raises here; the pivot path handles
    ``where`` columns before binding.
    """
    role = col.source.role
    if role == AxisRole.META_AXIS:
        raise RoleResolutionError(
            role=role.value,
            reason="a meta-axis output column needs a `where` predicate to select a member",
        )
    if role == AxisRole.VALUE:
        return AxisBinding(col.column, role, None, col.transform)
    if col.source.axis is None:
        axis_id = _single_axis(role, role_to_axes)
    else:
        candidates = role_to_axes.get(role, [])
        if col.source.axis not in candidates:
            raise RoleResolutionError(
                role=role.value,
                reason=(
                    f"column {col.column!r} addresses axis {col.source.axis!r}, but "
                    f"it is not classified as {role.value} in this table "
                    f"(axes with that role: {candidates or 'none'})"
                ),
            )
        axis_id = col.source.axis
    return AxisBinding(col.column, role, axis_id, col.transform)


def _resolve_source(
    binding: AxisBinding,
    lookup: dict[str, dict[str, str]],
) -> Callable[[dict[str, Any]], Any]:
    """Bind one resolved column (:class:`AxisBinding`) to a per-row reader that
    returns a *canonical cell*.

    The cell shape follows the column's role (the axis it reads was already
    resolved by :func:`_bind_axis`):

    * **VALUE** — a ``{value, unit}`` measure. The number is the observation
      cell (``value``), not an axis code — the classifier assigns VALUE to the
      single-member ``tab`` axis, but the magnitude lives under ``value`` in
      Layer 2's flattened row; its ``unit`` is the sibling cell. The declared
      transform runs on the magnitude.
    * **TIME** — a full :func:`time_cell` whose ``normalized`` / ``granularity``
      are driven by the column's *declared* format. ``best_effort_time``
      (the role-default) is total — an unrecognised code is kept raw. A
      declared *strict* format (e.g. ``monthly_e_stat``) that the data's shape
      violates raises a typed :class:`TimeFormatError`, routed by provenance
      (caller's rule surfaces, built-in degrades — ARCHITECTURE.md), so a
      declared format is honored rather than silently replaced by a guess.
    * **AREA / CATEGORY** — a ``{code, label}`` dimension. The label comes
      from the class metadata, falling back to the code; these are
      passthrough (no standard-code mapping).
    """
    role = binding.role
    if role == AxisRole.VALUE:
        transform = _resolve_transform(binding.column, binding.transform)

        def read(row: dict[str, Any]) -> Any:
            return measure(_apply_transform(transform, row.get("value")), row.get("unit"))

        return read

    axis_id = binding.axis_id
    codes = lookup.get(axis_id, {})
    if role == AxisRole.TIME:
        return _time_reader(binding, codes)

    # AREA / CATEGORY — passthrough dimension. Validate the transform name so a
    # typo still surfaces. No standard-code mapping for area here.
    _validate_transform(binding.column, binding.transform)

    def read(row: dict[str, Any]) -> Any:
        code = row.get(axis_id)
        return dimension(code, _label(codes, code))

    return read


def _time_reader(
    binding: AxisBinding, codes: dict[str, str]
) -> Callable[[dict[str, Any]], Any]:
    """Bind a TIME column to a reader that builds a :func:`time_cell` from the
    column's declared format.

    The declared transform drives the time object. Order matters: the name is
    validated first (a typo surfaces as :class:`UnknownTransformError`),
    then mapped to a :class:`TimePoint`-returning parser. A name that is a
    valid transform but not a time format is a :class:`TimeFormatError`.
    ``best_effort_time`` is total; a strict parser raising ``ValueError`` on a
    shape mismatch becomes a :class:`TimeFormatError` — both routed by
    provenance on the auto path (ARCHITECTURE.md).
    """
    _validate_transform(binding.column, binding.transform)  # typo → UnknownTransformError, first
    axis_id = binding.axis_id
    if binding.transform == "best_effort_time":
        # The total role-default is time_cell's auto-normalize, which
        # consults the member's display name — the only signal that
        # separates a year-span code from a month. Dispatched here,
        # at bind time, so the per-row reader has exactly one job (a
        # non-string code takes time_cell's same raw-keeping path).
        def read_best_effort(row: dict[str, Any]) -> Any:
            code = row.get(axis_id)
            return time_cell(code, _label(codes, code))

        return read_best_effort

    parser = TIME_PARSERS.get(binding.transform)
    if parser is None:
        raise TimeFormatError(
            column=binding.column,
            transform=binding.transform,
            reason=(
                "not a time format; a time column must declare best_effort_time "
                f"or a specific parser ({sorted(TIME_PARSERS)})"
            ),
        )

    def read(row: dict[str, Any]) -> Any:
        code = row.get(axis_id)
        label = _label(codes, code)
        if not isinstance(code, str):
            return time_cell(code, label, None)
        try:
            point = parser(code)
        except ValueError as exc:
            raise TimeFormatError(
                column=binding.column,
                transform=binding.transform,
                code=code,
                reason="code does not match the declared time format",
            ) from exc
        return time_cell(code, label, point)

    return read


def _single_axis(
    role: AxisRole, role_to_axes: dict[AxisRole, list[str]]
) -> str:
    """The one axis playing ``role``, or a typed error if absent / repeated.

    A role-addressed (no ``axis`` id) non-meta column must resolve to exactly
    one axis. Zero axes (a rule asking for a role the table lacks) and several
    axes (a repeated non-meta role, which is addressed by id instead — see
    :func:`_bind_axis`) both fail as typed
    :class:`RoleResolutionError`\\ s so the auto path can route to Layer D.
    """
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
                "address one by axis id (source.axis) to pick which column "
                "reads which"
            ),
        )
    return axes[0]
