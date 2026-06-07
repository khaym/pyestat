"""Output contract (Layer 3) — the canonical nested record and its flat
projection (#35).

The single home for "what one converted row looks like". Every conversion
path (Layer D, v2 generic/builtin 1:1, v2 pivot) builds its cells through
the constructors here, so the output shape cannot drift between paths — the
inconsistency #35 set out to remove (generic 1:1 dropping labels, time
metadata, and the observation value while Layer D kept them).

The **nested form is canonical**: each field is a self-describing object,
so an LLM agent reads ``row["cat01"]["label"]`` without knowing a suffix
convention. :func:`to_flat_rows` is the cheap, lossless projection back to
one column per field for callers (pandas users) who prefer the flat shape.

Field shapes:

* **dimension** — ``{"code", "label"}`` for a category / area / tab axis. A
  label-less code (trade HS codes, where ``code == name``) carries its code
  as the label, so the shape is never partial.
* **time** — ``{"code", "label", "normalized", "granularity"}``: the raw
  e-Stat code, e-Stat's display name, the ISO-leaning normalized string,
  and the granularity tag. A code no parser recognises keeps
  ``normalized == code`` and ``granularity is None`` so the object is always
  fully formed.
* **measure** — ``{"value", "unit"}`` for an observation cell. Wrapping the
  value with its unit keeps a pivoted table correct when measures carry
  different units (trade's 数量 in ＮＯ vs 金額 in 千円), which a single
  shared ``unit`` sibling could not represent.
"""
from __future__ import annotations

from typing import Any

from pyestat._engine.time import TimePoint, best_effort

# Sentinel: ``time_cell`` normalizes via best-effort when no parsed point is
# supplied (the total, no-rule path). Distinct from ``None``, which means "a
# parser was run and the code did not parse" — kept raw.
_AUTO_NORMALIZE = object()

# Key sets that identify each canonical cell when flattening. A raw row's
# cells are scalars, so they match none of these and pass through untouched.
_TIME_KEYS = frozenset({"code", "label", "normalized", "granularity"})
_DIMENSION_KEYS = frozenset({"code", "label"})
_MEASURE_KEYS = frozenset({"value", "unit"})


def dimension(code: Any, label: Any) -> dict[str, Any]:
    """A category / area / tab axis cell: its code and human label."""
    return {"code": code, "label": label}


def time_cell(
    code: Any, label: Any, point: "TimePoint | None" = _AUTO_NORMALIZE  # type: ignore[assignment]
) -> dict[str, Any]:
    """A time axis cell.

    Omit ``point`` (the default) to normalize ``code`` best-effort — the
    total, no-rule path (Layer D, and a v2 column whose format is the
    ``best_effort_time`` role-default): an unrecognised or non-string code
    keeps ``normalized == code`` and ``granularity is None`` rather than
    raising. Pass a :class:`TimePoint` when a *declared* format parsed the
    code, so the rule's chosen format drives both fields; pass ``None`` for a
    code a parser was run on but did not recognise (also kept raw). Either way
    the object is fully formed.
    """
    if point is _AUTO_NORMALIZE:
        point = best_effort(code) if isinstance(code, str) else None
    if point is None:
        return {"code": code, "label": label, "normalized": code, "granularity": None}
    return {
        "code": code,
        "label": label,
        "normalized": point.normalized,
        "granularity": point.granularity,
    }


def measure(value: Any, unit: Any) -> dict[str, Any]:
    """An observation cell: its value paired with its unit."""
    return {"value": value, "unit": unit}


def to_flat_rows(values: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """Project canonical nested rows to the flat suffix convention.

    Lossless and idempotent: each canonical cell expands to its legacy flat
    columns, and a scalar cell (a raw ``rule=None`` row, or an already-flat
    row) passes through unchanged — so calling this on any response shape is
    safe.
    """
    return tuple(_flatten_row(row) for row in values)


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, cell in row.items():
        if _is_cell(cell, _TIME_KEYS):
            # `time` axis → time/time_code/time_label/time_granularity (the
            # historical Layer D keys); a non-`time` axis read as time keeps
            # the same suffixes under its own key.
            _put(out, key, cell["normalized"])
            _put(out, f"{key}_code", cell["code"])
            _put(out, f"{key}_label", cell["label"])
            _put(out, f"{key}_granularity", cell["granularity"])
        elif _is_cell(cell, _DIMENSION_KEYS):
            _put(out, key, cell["code"])
            _put(out, f"{key}_label", cell["label"])
        elif _is_cell(cell, _MEASURE_KEYS):
            _put(out, key, cell["value"])
            # The lone observation column is named "value"; its unit takes
            # the bare `unit` key (legacy). A pivot measure column keeps a
            # per-column `{column}_unit` so differing units never collide.
            _put(out, "unit" if key == "value" else f"{key}_unit", cell["unit"])
        else:
            _put(out, key, cell)
    return out


def _put(out: dict[str, Any], key: str, value: Any) -> None:
    """Write a flat column, refusing to silently overwrite.

    The flat projection invents derived keys (``{K}_label``, the bare
    ``unit``, …) that the rule's own unique-column check cannot see, so two
    nested fields can map to one flat key (e.g. a ``value`` measure's bare
    ``unit`` and a sibling column literally named ``unit``). Rather than drop
    one silently, fail loud and name the collision so the rule author renames
    a column."""
    if key in out:
        raise ValueError(
            f"flat projection collision on column {key!r}: two nested fields "
            "map to the same flat key — rename one of the rule's output columns"
        )
    out[key] = value


def _is_cell(cell: Any, keys: frozenset[str]) -> bool:
    return isinstance(cell, dict) and cell.keys() == keys
