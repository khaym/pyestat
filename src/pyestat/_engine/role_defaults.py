"""Role-default registry and v2 transform registry (Layer A substance).

Two things live here, both feeding the output-schema-first v2 rules:

* **The transform registry** — the named, per-column transforms a v2 rule
  may reference: ``passthrough`` plus the existing time parsers (surfaced as
  transforms that emit the normalized string). Standard-code transforms
  (``iso8601`` / ``jis_x_0401`` / …) can be registered here later.
* **The role-default map** — for each :class:`AxisRole`, the transform a
  short-form column inherits when it names no transform of its own.

A load-bearing invariant from the design discussion: every *role-default*
is a **total** transform (never raises). A rule built purely from
role-defaults is exactly what Layer A generates for an uncovered table, so
its application must not be able to fail — an unrecognised code degrades to
its raw value instead of throwing, keeping Layer D as the only place the
auto path can lose structure.

Expansion (short form → long form) also lives here because it is the join
point of the two registries: it infers a missing ``source.role`` from the
column name (Decision 1A) and fills a missing ``transform`` from the
role-default map.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Callable

from pyestat._endpoint import ClassObj
from pyestat._engine.classifier import (
    AxisRole,
    TableClassification,
    is_flat_axis,
    pivot_member_name,
)
from pyestat._engine.registry import Registry
from pyestat._engine.rule import MatchV2, MetaWhere, OutputColumn, RoleSource, RuleV2
from pyestat._engine.time import (
    TimePoint,
    best_effort,
    fiscal_year_e_stat,
    monthly_e_stat,
    quarterly_e_stat,
    yearly,
)
from pyestat.errors import RuleExpansionError


# A transform maps one source value to its output cell. Kept deliberately
# 1:1 (value → value); the N:1 pivot lives above this layer.
Transform = Callable[[Any], Any]


def _passthrough(value: Any) -> Any:
    return value


def _normalizing(parser: Callable[[str], Any]) -> Transform:
    """Adapt a ``time.py`` parser into a transform that emits the
    normalized string (Decision 3 — a v2 time column is one value, not
    the v1 trio of normalized / code / granularity)."""

    def transform(value: Any) -> Any:
        return parser(value).normalized

    return transform


def _best_effort_time(value: Any) -> Any:
    """Total time transform: normalize if any parser recognises the code,
    otherwise return it untouched. This is the ``time`` role-default.

    *Total* means total over **any** input, not just strings: ``best_effort``
    is documented to take a ``str`` and feeds the regex parsers, which raise
    ``TypeError`` on a non-string (an int year a YAML/JSON layer coerced, a
    caller-built row). A role-default that raised would void the Layer A
    "never lose structure" guarantee, so a non-string degrades to its raw
    value here rather than reaching the parsers."""
    if not isinstance(value, str):
        return value
    point = best_effort(value)
    return point.normalized if point is not None else value


TRANSFORMS: Registry[Transform] = Registry(kind="transform")
TRANSFORMS.register("passthrough", _passthrough)
TRANSFORMS.register("monthly_e_stat", _normalizing(monthly_e_stat))
TRANSFORMS.register("quarterly_e_stat", _normalizing(quarterly_e_stat))
TRANSFORMS.register("yearly", _normalizing(yearly))
TRANSFORMS.register("fiscal_year_e_stat", _normalizing(fiscal_year_e_stat))
TRANSFORMS.register("best_effort_time", _best_effort_time)


# Time-format transforms that yield a full ``TimePoint`` (normalized value +
# granularity), keyed by the same names the scalar ``TRANSFORMS`` registry
# uses. The canonical time cell is built from these so a column's
# *declared* format drives both fields. The strict parsers raise
# ``ValueError`` on a code whose shape does not match, which the apply path
# turns into a typed ``TimeFormatError`` routed by provenance (a caller's
# rule surfaces, a built-in degrades — ARCHITECTURE.md). ``best_effort_time`` is
# the total role-default; the apply path dispatches it at bind time to
# ``time_cell``'s auto-normalize (which consults the member name), so
# its entry here marks membership — code-only callers can still resolve it
# to ``best_effort``.
TIME_PARSERS: dict[str, Callable[[str], "TimePoint | None"]] = {
    "best_effort_time": best_effort,
    "yearly": yearly,
    "fiscal_year_e_stat": fiscal_year_e_stat,
    "monthly_e_stat": monthly_e_stat,
    "quarterly_e_stat": quarterly_e_stat,
}


# Per-role default transform. Only ``time`` needs a non-trivial default
# (a granularity-agnostic best-effort parse); every other role passes its
# value through (standard-code mapping for ``area`` is out of scope here).
# All entries resolve to total transforms — see the module docstring.
_ROLE_DEFAULT_TRANSFORM: dict[AxisRole, str] = {
    AxisRole.TIME: "best_effort_time",
}
_FALLBACK_TRANSFORM = "passthrough"


def default_transform(role: AxisRole) -> str:
    """The transform name a short-form column of ``role`` inherits."""
    return _ROLE_DEFAULT_TRANSFORM.get(role, _FALLBACK_TRANSFORM)


# Roles a bare column name may infer (Decision 1A). Excludes the two roles
# that are not *directly addressable* output sources: ``unknown`` is a
# classifier sentinel (never a real source), and ``meta-axis`` needs a
# ``where`` predicate to pick a value (pivot) so it cannot be a bare
# short-form column. Both must be spelled out with an explicit source.
_INFERABLE_ROLES: frozenset[AxisRole] = frozenset(
    {AxisRole.TIME, AxisRole.AREA, AxisRole.VALUE, AxisRole.CATEGORY}
)


def _role_from_column_name(column: str) -> AxisRole:
    """Decision 1A: a bare column name doubles as its role. A name that is
    not an inferable role is an authoring error — the column must spell out
    its ``source`` — and fails loud here rather than silently never firing
    or binding to a sentinel/pivot role."""
    try:
        role = AxisRole(column)
    except ValueError:
        role = None
    if role is None or role not in _INFERABLE_ROLES:
        raise RuleExpansionError(
            column=column,
            reason=(
                "no source given and the column name is not a directly "
                "addressable role; add an explicit source (inferable roles: "
                f"{[r.value for r in _INFERABLE_ROLES]})"
            ),
        )
    return role


def _expand_column(col: OutputColumn) -> OutputColumn:
    source = col.source if col.source is not None else RoleSource(
        role=_role_from_column_name(col.column)
    )
    transform = col.transform if col.transform is not None else default_transform(
        source.role
    )
    return OutputColumn(column=col.column, source=source, transform=transform)


def expand_short_form(rule: RuleV2) -> RuleV2:
    """Return ``rule`` with every column in long form.

    Idempotent: a fully-specified column expands to itself, so callers may
    expand defensively without tracking whether the loader already did.
    """
    return rule.model_copy(
        update={"output": [_expand_column(col) for col in rule.output]}
    )


def build_generic_rule(
    classification: TableClassification,
    class_objs: Sequence[ClassObj] | None = None,
) -> RuleV2 | None:
    """Build a Layer A generic rule from a classification, or ``None`` when
    the table cannot be structured generically and must route to Layer D.

    Two shapes are generated. A table with **no meta-axis** maps each axis to
    one 1:1 column: the ``value`` role reads the observation cell, every other
    role reads its own axis (addressed by axis id, so two axes of one role —
    建築主 × 用途 — each get a column). A table with **exactly one flat
    meta-axis** is pivoted: the non-meta axes stay 1:1 and the meta-axis
    is folded into one ``where`` column per member, so the table comes back one
    record per non-meta group (column = the member's NFKC-normalized name).
    Naming the pivot columns needs the member names, so a meta-axis table
    declines without ``class_objs``.

    Returns ``None`` (→ Layer D) when the table cannot be structured *or* the
    meta-axis is not safe to flat-pivot: an ``unknown`` axis (the classifier's
    route-to-D sentinel), **two or more** meta-axes (folding several needs
    explicit disambiguation), a **hierarchical meta-axis** (its
    members carry a code hierarchy that folds a second dimension into them —
    trade's measure×period cross; flat-pivoting would spread it into columns,
    so it rides Layer D and a rule reshapes it — see
    :func:`classifier.is_flat_axis`), a **VALUE role coexisting with the
    meta-axis** (the measure is already spread across the meta-axis, so a
    separate ``value`` column would read an arbitrary group member), or a
    **column-name collision** (a meta member named like a non-meta column, or an
    axis id'd ``value``) — declining rather than letting :class:`RuleV2`'s
    duplicate-column validator raise on the auto path, which would break the
    "auto never raises" guarantee. (Several meta members sharing one name do
    *not* collide — they coalesce into one column; see :func:`_pivot_columns`.)

    Every role-default is a total transform (see module docstring) and pivot
    columns are ``passthrough``, so no *transform* raises at apply time. The
    pivot's member selection can still raise a typed :class:`RuleAuthoringError`
    — a ``where`` matching several members that disagree on their value (trade's
    measure×period cross; or duplicate-name members that are not a benign
    equal-valued coalesce) — which the auto path degrades to Layer D for a
    library-provided rule (ARCHITECTURE.md), so a generic rule never crashes the
    request: the Layer A guarantee.
    """
    axes = classification.axes
    if not axes:
        return None
    roles = [axis.role for axis in axes]
    if AxisRole.UNKNOWN in roles:
        return None
    meta_axes = [axis for axis in axes if axis.role == AxisRole.META_AXIS]
    if len(meta_axes) > 1:
        return None
    non_meta = [axis for axis in axes if axis.role != AxisRole.META_AXIS]
    non_meta_roles = [axis.role for axis in non_meta]
    # On the 1:1 (no-meta) shape the observation cell exists on every e-Stat
    # row whether or not a tab axis describes it, so the column set
    # must read it exactly once; on the pivot shape the observation lives in
    # each member's where-column instead.
    nonmeta_cols = _one_to_one_columns(non_meta, ensure_value=not meta_axes)
    if nonmeta_cols is None:
        return None
    match = MatchV2(role_pattern=roles)
    if not meta_axes:
        return RuleV2(schema_version="2", match=match, output=nonmeta_cols)
    if AxisRole.VALUE in non_meta_roles:
        # A meta-axis already spreads the measures across rows, so the
        # observation lives in each member's cell. A coexisting single-member
        # VALUE (tab) axis would also emit a 1:1 ``value`` column, which after
        # grouping reads an arbitrary group member's cell (the pivot's
        # representative row) — a spurious, non-deterministic duplicate. This
        # shape (a value type *and* a measure spread) is unexpected, so decline
        # to Layer D rather than fold it ambiguously.
        return None
    meta_cols = _pivot_columns(meta_axes[0], class_objs)
    if meta_cols is None:
        return None
    output = nonmeta_cols + meta_cols
    if len({col.column for col in output}) != len(output):
        # A meta member name collides with a non-meta column name (e.g. a
        # member literally named "area"). Decline (→ Layer D) rather than
        # raise from RuleV2's duplicate-column validator on the auto path.
        return None
    return RuleV2(schema_version="2", match=match, output=output)


def _one_to_one_columns(
    axes: Sequence[Any], *, ensure_value: bool = False
) -> list[OutputColumn] | None:
    """One 1:1 column per non-meta axis, or ``None`` on a name collision.

    The ``value`` role becomes the ``value`` column (reading the observation
    cell, so it carries no axis); every other axis reads its own id and is
    *addressed by that id*, so two axes of one role — 建築主 × 用途 —
    resolve to distinct columns instead of colliding on the role. With
    ``ensure_value`` (the 1:1 rule shape) the column set always reads the
    observation cell exactly once: when no axis carries the VALUE role, a
    synthesized ``value`` column is appended — the observation exists on every
    e-Stat row whether or not a tab axis describes it, and without the column it
    silently vanishes. A non-value axis id'd ``value`` would collide with that
    column either way, so the rule declines (→ Layer D) instead of building a
    duplicate.
    """
    # (axis_id, role) per column. The VALUE role reads the observation cell, not
    # an axis, so it carries no axis id — decided here, once, so the column's
    # name and source agree without re-checking the role downstream.
    specs: list[tuple[str | None, Any]] = [
        (None if axis.role == AxisRole.VALUE else axis.axis_id, axis.role)
        for axis in axes
    ]
    if ensure_value and AxisRole.VALUE not in (role for _axis_id, role in specs):
        specs.append((None, AxisRole.VALUE))
    names = ["value" if role == AxisRole.VALUE else axis_id for axis_id, role in specs]
    if len(set(names)) != len(names):
        return None
    return [
        OutputColumn(
            column=name,
            source=RoleSource(role=role, axis=axis_id),
            transform=default_transform(role),
        )
        for (axis_id, role), name in zip(specs, names)
    ]


def _pivot_columns(
    meta_axis: Any, class_objs: Sequence[ClassObj] | None
) -> list[OutputColumn] | None:
    """One ``where`` column per meta-axis member, or ``None`` when the members
    cannot be named into distinct columns (so the table rides Layer D).

    Each column selects a member by its NFKC-normalized name — the same fold
    the pivot apply path applies when matching ``where`` — so the
    generated selector and the member it targets cannot drift. Declines when
    the meta-axis is **hierarchical** (its members carry a code hierarchy, so a
    second dimension is folded into them — trade's 合計/月次 × 数量/金額; a flat
    pivot would spread it into columns flatness gate) or when no member
    names are available (no ``class_objs`` or none for this axis: the columns
    would be unnamed and the measure silently dropped).

    **Several members sharing one name coalesce into a single column**.
    賃金構造 "DB" tables carry each measure twice — same name+unit under a second
    code block (a code-scheme vintage, not a second dimension); each cell
    populates one block, the one overlap year dual-codes identical values. One
    ``where: {equals: name}`` column per *distinct* name lets the apply path
    pick whichever block's member the cell carries (and coalesce the identical
    overlap), so the table folds instead of declining on the name repeat — while
    members that genuinely disagree still surface the apply-time ambiguity that
    routes to Layer D.
    """
    if class_objs is None:
        return None
    obj = next((o for o in class_objs if o.id == meta_axis.axis_id), None)
    if obj is None or not is_flat_axis(obj):
        return None
    # De-duplicate by name, preserving first-seen order: same-named members are
    # the same measure, so they share one column the apply path coalesces.
    members = list(dict.fromkeys(pivot_member_name(c) for c in obj.classes if "code" in c))
    if not members:
        return None
    return [
        OutputColumn(
            column=name,
            source=RoleSource(role=AxisRole.META_AXIS, where=MetaWhere(equals=name)),
            transform=_FALLBACK_TRANSFORM,
        )
        for name in members
    ]
