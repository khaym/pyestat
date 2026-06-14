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
from pyestat.errors import (
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
    resolved: "ResolvedRule | None",
) -> tuple[dict[str, Any], ...]:
    """Apply the ``rule="auto"`` decision, surfacing or degrading an
    application failure by the resolved rule's provenance.

    ``resolved`` is the resolver's output: a :class:`ResolvedRule` (the rule
    paired with the layer it came from) or ``None`` (route to Layer D). When
    applying the rule fails with a :class:`RuleAuthoringError` (a role
    missing or ambiguous, a short-form column that cannot expand, or a
    transform name the registry lacks), the layer decides what happens: a
    caller-authored rule (user / project) surfaces the error so the caller
    can fix it; a library-provided rule (builtin / generic) degrades to
    Layer D, since the caller cannot fix it and preserved data beats a
    crash. Catching the shared base means a future authoring-error leaf is
    routed by the same policy without editing this clause. See
    ``docs/DESIGN.md`` Decision B.

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
    """Layer D — the no-rule fallback (#23): preserve data, normalize nothing
    structural.

    The axis ``classification`` (computed once on the request path, #28, not
    a hand-written rule) decides which axis is ``time``. Output is the
    canonical nested form (#35): every classified axis becomes a
    ``{code, label}`` cell — the ``time`` axis a full :func:`time_cell` — and
    the observation becomes a ``{value, unit}`` measure. Raw codes stay in
    each cell's ``code``, the value is never coerced, and a code no parser
    recognises keeps ``normalized == code`` — Layer D never raises and never
    drops a row. ``area`` is passed through as a plain dimension;
    standard-code mapping is task #4's job.
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
    plan = [(col.column, _resolve_source(col, role_to_axes, lookup)) for col in rule.output]
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
    grain a ``key`` derives (#10, #37).

    Groups ``values`` by the codes of every non-meta axis. A ``key`` column
    (#37) lifts a value out of each member's *name* (the trade cross encodes
    the month only there, e.g. ``"1月_金額"``) and adds it to the grain, so one
    group can emit several rows — one per derived key. Within each
    (group, grain) record:

    * non-meta columns read the group's shared codes as canonical cells (#35);
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
    # Three member→signal maps a `where` predicate reads. Parent is resolved to
    # the parent member's *name* (the vocabulary `where: {parent}` speaks), so a
    # measure family is selectable without naming each child.
    name_by_code = {code: pivot_member_name(c) for code, c in members.items()}
    parent_name_by_code = {
        code: (name_by_code.get(c["parentCode"]) if c.get("parentCode") else None)
        for code, c in members.items()
    }
    level_by_code = {code: str(c.get("level", "")) for code, c in members.items()}

    group_axis_ids = [
        a.axis_id for a in classification.axes if a.role != AxisRole.META_AXIS
    ]
    nonmeta_plan = [
        (col.column, _resolve_source(col, role_to_axes, lookup))
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
            _member_predicate(
                col.source.where, name_by_code, parent_name_by_code, level_by_code
            ),
            (
                _member_predicate(
                    col.source.unit_from,
                    name_by_code,
                    parent_name_by_code,
                    level_by_code,
                )
                if col.source.unit_from is not None
                else None
            ),
            _resolve_transform(col.column, col.transform),
        )
        for col in rule.output
        if col.source.where is not None
    ]

    def grain_of(row: dict[str, Any]) -> tuple:
        """The derived-key tuple for a row, reading each `key` pattern against
        the member's name. A pattern that does not match yields ``None`` for
        that component — the member sits outside the derived grain (a level-1
        total under a month key) and forms no row of its own.

        The grain value is the first capture group, or the whole match when the
        pattern declares none. A pattern *with* a group that did not participate
        (an alternation/optional branch) falls back to the whole match too, so a
        member whose name the pattern *did* match is never silently dropped for
        an empty group."""
        name = name_by_code.get(row.get(meta_id), "")
        keys = []
        for _, pattern in key_plan:
            match = pattern.search(name)
            if match is None:
                keys.append(None)
                continue
            captured = match.group(1) if pattern.groups else match.group(0)
            keys.append(captured if captured is not None else match.group(0))
        return tuple(keys)

    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in values:
        key = tuple(row.get(aid) for aid in group_axis_ids)
        groups.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for rows in groups.values():
        base = {column: read(rows[0]) for column, read in nonmeta_plan}
        if key_plan:
            by_grain: dict[tuple, list[dict[str, Any]]] = {}
            for row in rows:
                grain = grain_of(row)
                if any(value is None for value in grain):
                    continue  # member outside the derived grain (e.g. a total)
                by_grain.setdefault(grain, []).append(row)
            grain_records = list(by_grain.items())
        else:
            grain_records = [((), list(rows))]
        for grain, candidates in grain_records:
            record = dict(base)
            for (column, _), value in zip(key_plan, grain):
                record[column] = value
            for column, predicate, unit_predicate, transform in where_plan:
                matched = [r for r in candidates if predicate(r.get(meta_id))]
                # Count *distinct* members, not rows: a member duplicated within
                # one group (a malformed response, or an axis outside the role
                # pattern) collapses to its first row — the pre-#37 graceful
                # behavior — while several *different* members matching one
                # predicate is the genuine ambiguity a `key` must split.
                distinct = {r.get(meta_id) for r in matched}
                if len(distinct) > 1:
                    raise RoleResolutionError(
                        role="meta-axis",
                        reason=(
                            f"the `where` for column {column!r} matched "
                            f"{len(distinct)} distinct members in one record; a "
                            "where selects a single member — add a `key` to "
                            "split them into rows"
                        ),
                    )
                if not matched:
                    record[column] = None
                else:
                    row = matched[0]
                    # `unit_from` (#39) overrides the value row's own @unit with
                    # a grain-less member's value, read from the whole group
                    # (`rows`) — the unit member (単位2) carries no period grain,
                    # so it lives outside `candidates`. Absent it, the column
                    # keeps its own @unit.
                    unit = (
                        _broadcast_unit(column, unit_predicate, rows, meta_id)
                        if unit_predicate is not None
                        else row.get("unit")
                    )
                    record[column] = measure(
                        _apply_transform(transform, row.get("value")), unit
                    )
            out.append(record)
    return tuple(out)


def _broadcast_unit(
    column: str,
    predicate: Callable[[Any], bool],
    pool: Sequence[dict[str, Any]],
    meta_id: str,
) -> Any:
    """The unit string a ``unit_from`` (#39) broadcasts into a measure: the
    *value* of the one grain-less member its predicate selects across the
    group.

    Trade ships a quantity's unit as a level-1 member (``単位2``) whose
    observation value *is* the unit (``"ＮＯ"``), not an ``@unit`` attribute —
    so the unit is read from ``value``. ``pool`` is the whole non-meta group
    (not the period grain), because that member sits outside the grain. A
    predicate matching no member yields ``None`` — the same graceful stance a
    ``where`` matching nothing takes — while several *distinct* members is an
    ambiguity the author must narrow (mirrors the ``where`` multi-match error).
    """
    matched = [r for r in pool if predicate(r.get(meta_id))]
    distinct = {r.get(meta_id) for r in matched}
    if len(distinct) > 1:
        raise RoleResolutionError(
            role="meta-axis",
            reason=(
                f"the `unit_from` for column {column!r} matched {len(distinct)} "
                "distinct members; a unit_from selects a single unit member — "
                "narrow it (equals / parent / level)"
            ),
        )
    return matched[0].get("value") if matched else None


def _member_predicate(
    where: "MetaWhere",
    name_by_code: dict[str, str],
    parent_name_by_code: dict[str, str | None],
    level_by_code: dict[str, str],
) -> Callable[[Any], bool]:
    """Compile a ``where`` into a predicate over a meta member's code: ``True``
    when the member satisfies *every* selector given (AND).

    Names compare NFKC-folded (the maps already applied the fold the classifier
    used, so a selector and the member it targets cannot drift); ``level``
    compares as the raw ``@level`` string. Each selector binds its target at
    definition so the closures do not share one loop variable.
    """
    checks: list[Callable[[Any], bool]] = []
    if where.equals is not None:
        checks.append(
            lambda code, target=_norm(where.equals): name_by_code.get(code) == target
        )
    if where.parent is not None:
        checks.append(
            lambda code, target=_norm(where.parent): parent_name_by_code.get(code)
            == target
        )
    if where.level is not None:
        checks.append(lambda code, target=where.level: level_by_code.get(code) == target)
    return lambda code: all(check(code) for check in checks)


def _resolve_transform(column: str, name: str) -> Callable[[Any], Any]:
    """Resolve a transform name, converting the registry's ``KeyError`` into
    a typed :class:`UnknownTransformError` that names the column.

    The registry raises a ``KeyError`` subclass; left bare it would escape
    the auto path's typed-error handling and crash the caller. Wrapping it
    here — the one place a rule's transform name meets the registry — keeps
    the contract that an unknown transform is a typed, provenance-routed
    authoring error (see ``docs/DESIGN.md`` Decision B), not a stray
    ``KeyError``.
    """
    try:
        return TRANSFORMS.resolve(name)
    except RegistryKeyError as exc:
        raise UnknownTransformError(
            column=column, transform=name, known=TRANSFORMS.names()
        ) from exc


def _validate_transform(column: str, name: str) -> None:
    """Assert a transform name is known (typo → :class:`UnknownTransformError`,
    #32) *without* binding the callable — for a column whose canonical cell is
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


def _resolve_source(
    col: OutputColumn,
    role_to_axes: dict[AxisRole, list[str]],
    lookup: dict[str, dict[str, str]],
) -> Callable[[dict[str, Any]], Any]:
    """Bind one output column to a per-row reader that returns a *canonical
    cell* (#35), resolved once per call.

    The cell shape follows the column's role:

    * **VALUE** — a ``{value, unit}`` measure. The number is the observation
      cell (``value``), not an axis code — the classifier assigns VALUE to the
      single-member ``tab`` axis, but the magnitude lives under ``value`` in
      Layer 2's flattened row; its ``unit`` is the sibling cell. The declared
      transform runs on the magnitude.
    * **TIME** — a full :func:`time_cell` whose ``normalized`` / ``granularity``
      are driven by the column's *declared* format (#35). ``best_effort_time``
      (the role-default) is total — an unrecognised code is kept raw. A
      declared *strict* format (e.g. ``monthly_e_stat``) that the data's shape
      violates raises a typed :class:`TimeFormatError`, routed by provenance
      (caller's rule surfaces, built-in degrades — Decision B), so a declared
      format is honored rather than silently replaced by a best-effort guess.
    * **AREA / CATEGORY** — a ``{code, label}`` dimension. The label comes
      from the class metadata, falling back to the code. (#4 will add a
      standard-code field additively; today these are passthrough.)

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
    if role == AxisRole.VALUE:
        transform = _resolve_transform(col.column, col.transform)

        def read(row: dict[str, Any]) -> Any:
            return measure(_apply_transform(transform, row.get("value")), row.get("unit"))

        return read

    axis_id = _resolve_axis(col, role_to_axes)
    codes = lookup.get(axis_id, {})
    if role == AxisRole.TIME:
        return _time_reader(col, axis_id, codes)

    # AREA / CATEGORY — passthrough dimension. Validate the transform name so a
    # typo still surfaces (#32); #4 will give area a standard-code field.
    _validate_transform(col.column, col.transform)

    def read(row: dict[str, Any]) -> Any:
        code = row.get(axis_id)
        return dimension(code, _label(codes, code))

    return read


def _time_reader(
    col: OutputColumn, axis_id: str, codes: dict[str, str]
) -> Callable[[dict[str, Any]], Any]:
    """Bind a TIME column to a reader that builds a :func:`time_cell` from the
    column's declared format.

    The declared transform drives the time object. Order matters: the name is
    validated first (a typo surfaces as :class:`UnknownTransformError`, #32),
    then mapped to a :class:`TimePoint`-returning parser. A name that is a
    valid transform but not a time format is a :class:`TimeFormatError`.
    ``best_effort_time`` is total; a strict parser raising ``ValueError`` on a
    shape mismatch becomes a :class:`TimeFormatError` — both routed by
    provenance on the auto path (Decision B).
    """
    _validate_transform(col.column, col.transform)  # typo → UnknownTransformError, first
    if col.transform == "best_effort_time":
        # The total role-default is time_cell's auto-normalize, which
        # consults the member's display name — the only signal that
        # separates a year-span code from a month (#33). Dispatched here,
        # at bind time, so the per-row reader has exactly one job (a
        # non-string code takes time_cell's same raw-keeping path).
        def read_best_effort(row: dict[str, Any]) -> Any:
            code = row.get(axis_id)
            return time_cell(code, _label(codes, code))

        return read_best_effort

    parser = TIME_PARSERS.get(col.transform)
    if parser is None:
        raise TimeFormatError(
            column=col.column,
            transform=col.transform,
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
                column=col.column,
                transform=col.transform,
                code=code,
                reason="code does not match the declared time format",
            ) from exc
        return time_cell(code, label, point)

    return read


def _resolve_axis(
    col: OutputColumn, role_to_axes: dict[AxisRole, list[str]]
) -> str:
    """The axis a non-meta column reads from: the one it names, or — when it
    names none — the single axis of its role.

    Id addressing (#38) is how a repeated non-meta role is resolved: 建築主 ×
    用途 are two ``category`` axes, and each column names which one it draws
    from. The named axis must actually carry the column's role; addressing one
    that does not (a typo, or a role the table lacks) is a typed
    :class:`RoleResolutionError` naming the axis, so the auto path routes to
    Layer D rather than reading every row's missing cell as ``None``. With no
    axis named, fall back to :func:`_single_axis` (the role must be unique).
    """
    role = col.source.role
    if col.source.axis is None:
        return _single_axis(role, role_to_axes)
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
    return col.source.axis


def _single_axis(
    role: AxisRole, role_to_axes: dict[AxisRole, list[str]]
) -> str:
    """The one axis playing ``role``, or a typed error if absent / repeated.

    A role-addressed (no ``axis`` id) non-meta column must resolve to exactly
    one axis. Zero axes (a rule asking for a role the table lacks) and several
    axes (a repeated non-meta role, which is addressed by id instead — see
    :func:`_resolve_axis`, #38) both fail as typed
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
                "reads which (#38)"
            ),
        )
    return axes[0]
