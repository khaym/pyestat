"""Aggregate vs. detail row selection.

e-Stat encodes a code hierarchy with ``@parentCode``: a member that another
member names as its parent has children — it is an *aggregate* (a total or
subtotal: 総数, 大分類, 全国). A member with no children is a *leaf* — a
*detail* row. Summing a measure across a mix of aggregate and leaf rows
double-counts (食料 plus its 品目 plus 総数), so a caller filtering to leaves
(``"exclude"`` the aggregates) selects a single, self-consistent grain safe
to aggregate; filtering to aggregates (``"only"``) selects the rolled-up
figures.

Two deliberate choices, both deterministic:

* **Per-response, not absolute.** The parent links present in *this* table
  decide. A table holding only a total (no children fetched) names no parent,
  so nothing is an aggregate and nothing is dropped — there is no
  double-counting with a single grain. The flip side is the contract's edge:
  a hierarchy e-Stat ships *without* ``@parentCode`` (a flat 男女別 総数 / 男 /
  女) is invisible here and stays unfiltered.
* **Leaf on every dimension (AND).** Across several hierarchical axes
  (建築主 × 用途) a row is detail only when it is a leaf on *all* of them — the
  safe grain for the cross. ``"only"`` is the exact complement (an aggregate
  on at least one axis), so the two selections partition the rows.

Only the dimension axes (``category`` / ``area``) range over the selection:
``time`` granularity is the time normalizer's concern, a ``meta-axis`` hierarchy is the
pivot's to fold, and a ``value`` axis carries no code hierarchy. This
keeps the selection orthogonal to the conversion rule — it filters the raw
rows before any rule runs, so ``"auto"``, a built-in, a custom rule, and raw
mode all honor it uniformly.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, get_args

from pyestat._endpoint import ClassObj
from pyestat._engine.classifier import AxisRole, TableClassification

AggregateSelection = Literal["include", "exclude", "only"]

# The roles whose code hierarchy this selection ranges over. time is
# granularity, a meta-axis is the pivot's domain, value carries no
# codes — so the dimension roles are the only ones a parent/leaf split applies
# to.
_DIMENSION_ROLES = frozenset({AxisRole.CATEGORY, AxisRole.AREA})


def _aggregate_codes(axis: ClassObj, present: set[Any]) -> set[str]:
    """The codes on ``axis`` that have a child *present in the fetched rows* —
    the aggregates whose presence alongside their children would double-count.

    Data-driven on purpose (see the module docstring): a parent is an aggregate
    only when one of its children is actually in ``present``. A total fetched
    on its own names no present child, so it is a leaf here and is kept. The
    child itself need not have its own parent present — 食料 is still a subtotal
    over the 品目 below it even if 総数 was not fetched.
    """
    parent_of = {
        str(c["code"]): str(c["parentCode"])
        for c in axis.classes
        if "code" in c and c.get("parentCode") not in (None, "")
    }
    return {
        parent_of[str(code)]
        for code in present
        if code is not None and str(code) in parent_of
    }


def select_rows(
    values: Sequence[Mapping[str, Any]],
    classification: TableClassification,
    class_objs: Sequence[ClassObj],
    selection: AggregateSelection,
) -> tuple[dict[str, Any], ...]:
    """Filter ``values`` to detail rows, aggregate rows, or all.

    * ``"include"`` — every row, unchanged (the default; backward compatible).
    * ``"exclude"`` — drop the aggregates: keep rows that are a leaf on every
      hierarchical dimension axis.
    * ``"only"`` — keep the aggregates: the complement of ``"exclude"``.

    Aggregates are detected from ``@parentCode`` on the ``category`` / ``area``
    axes only (see the module docstring). A table whose dimensions encode no
    hierarchy has no aggregates, so ``"exclude"`` returns every row and
    ``"only"`` returns none. Rows are returned in input order; the filtered
    tuple holds the original row objects (this is a pure filter).
    """
    if selection not in get_args(AggregateSelection):
        raise ValueError(
            f"`aggregates` must be one of {get_args(AggregateSelection)}, got {selection!r}"
        )
    if selection == "include":
        return tuple(values)

    dimension_axes = {
        a.axis_id for a in classification.axes if a.role in _DIMENSION_ROLES
    }
    # Per dimension axis, the aggregate codes whose children are present in the
    # fetched rows. An axis with no such aggregate (flat, or only leaves
    # fetched) imposes nothing — every code on it is a leaf.
    parents_by_axis: dict[str, set[str]] = {}
    for obj in class_objs:
        if obj.id not in dimension_axes:
            continue
        present = {row.get(obj.id) for row in values}
        aggregates = _aggregate_codes(obj, present)
        if aggregates:
            parents_by_axis[obj.id] = aggregates
    if not parents_by_axis:
        # Nothing in this table is an aggregate: exclude keeps every (detail)
        # row, only keeps none.
        return tuple(values) if selection == "exclude" else ()

    keep_detail = selection == "exclude"
    return tuple(
        row
        for row in values
        if _is_detail(row, parents_by_axis) == keep_detail
    )


def _is_detail(row: Mapping[str, Any], parents_by_axis: Mapping[str, set[str]]) -> bool:
    """True when ``row`` is a leaf on *every* hierarchical dimension axis — the
    pure-detail grain. A row that is an aggregate on any one axis is not
    detail."""
    return all(
        str(row.get(axis_id)) not in parents
        for axis_id, parents in parents_by_axis.items()
    )
