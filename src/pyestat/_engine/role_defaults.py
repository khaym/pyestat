"""Role-default registry and v2 transform registry (Layer A substance, #22).

Two things live here, both feeding the output-schema-first v2 rules:

* **The transform registry** — the named, per-column transforms a v2 rule
  may reference. MVP ships the floor the design scopes: ``passthrough``
  plus the existing time parsers (surfaced as transforms that emit the
  normalized string, per Decision 3). #4 later registers the real
  standard-code transforms (``iso8601`` / ``jis_x_0401`` / …).
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

from typing import Any, Callable

from pyestat._engine.classifier import AxisRole, TableClassification
from pyestat._engine.registry import Registry
from pyestat._engine.rule import MatchV2, OutputColumn, RoleSource, RuleV2
from pyestat._engine.time import best_effort, monthly_e_stat, quarterly_e_stat, yearly
from pyestat.errors import RuleExpansionError


# A transform maps one source value to its output cell. Kept deliberately
# 1:1 (value → value); N:1 pivot is #10 and lives above this layer.
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
TRANSFORMS.register("best_effort_time", _best_effort_time)


# Per-role default transform. Only ``time`` needs a non-trivial default
# (a granularity-agnostic best-effort parse); every other role passes its
# value through until #4 gives ``area`` a standard-code mapper. All entries
# resolve to total transforms — see the module docstring.
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
# ``where`` predicate to pick a value (pivot, #10) so it cannot be a bare
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


def build_generic_rule(classification: TableClassification) -> RuleV2 | None:
    """Build a Layer A generic rule from a classification, or ``None`` when
    the table cannot be structured generically and must route to Layer D.

    Returns ``None`` when any axis is a ``meta-axis`` (folding it into one
    record needs an explicit #10 pivot rule, which Layer A never generates)
    or ``unknown`` (the classifier's route-to-Layer-D sentinel), or when a
    role repeats across axes (no way to address one of several same-role
    axes yet — #10's ``where`` disambiguates a meta-axis, not this case).
    Otherwise emits one long-form column per axis: the ``value``
    role reads the observation cell, every other role reads its own axis,
    and each inherits its role-default transform. Because every default is
    a total transform (see module docstring), the resulting rule cannot
    raise at apply time — the Layer A guarantee.
    """
    axes = classification.axes
    if not axes:
        return None
    roles = [axis.role for axis in axes]
    if any(role in (AxisRole.META_AXIS, AxisRole.UNKNOWN) for role in roles):
        return None
    if len(set(roles)) != len(roles):
        return None
    names = ["value" if axis.role == AxisRole.VALUE else axis.axis_id for axis in axes]
    if len(set(names)) != len(names):
        # A non-value axis whose id collides with the value column's name
        # (e.g. an axis literally id'd "value"). Decline rather than let
        # RuleV2's duplicate-column validator raise on the auto path, which
        # would break the "auto never raises" guarantee.
        return None
    output = [
        OutputColumn(
            column=name,
            source=RoleSource(role=axis.role),
            transform=default_transform(axis.role),
        )
        for name, axis in zip(names, axes)
    ]
    return RuleV2(schema_version="2", match=MatchV2(role_pattern=roles), output=output)
