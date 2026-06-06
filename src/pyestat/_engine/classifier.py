"""Axis classifier — Layer A keystone (PROPOSAL-AXIS-ROLE-INFERENCE, #21).

Given a table's axis metadata, label each axis with a *role* and a
discrete *confidence tier*. The classifier judges **structure** (is an
axis a time / area / value / meta-axis, or a plain category?) — not
semantics: which meta-axis value maps to which named output column is a
conversion-rule (#22) concern, and unit / aggregate are per-code
attributes consumed downstream (#4 / pivot), not axis roles.

Two deliberate design choices, both from the accepted proposal:

* **Deterministic heuristic, no LLM on the data path** (Open question 1).
  Roles fall out of e-Stat conventions (`time` / `area` axis ids), code
  shapes (10-digit dates vs 5-digit JIS), and the 表章項目 (`tab`)
  convention. The same metadata always yields the same classification.
* **Confidence is a discrete tier, not a probability** (Open question 5).
  A tier records how many independent signals agreed: ``high`` when a
  role's defining signals concur, ``medium`` for a single strong signal,
  ``low`` when signals conflict or only the weakest evidence is present.

The hard role is ``meta-axis`` (an axis that splits one logical record
across rows). Detecting it from a *non-tab* axis relies on a deliberately
narrow measure-spread lexicon (数量 / 金額 / 単位 / 価額) in the axis or
member names. This is the **mis-pivot guard**: ``@unit`` presence is *not*
used as a trigger — population's 男女別 axis carries heterogeneous
``@unit`` yet is a category, and pivoting it would silently corrupt data.
An ambiguous meta candidate is left ``unknown`` / ``low`` so the table
falls to Layer D (raw rows preserved, #23) rather than being mis-pivoted.

This module is a pure classifier. Routing the result — rule resolution
by role pattern (#22), the Layer D fallback and ``rule="auto"`` semantics
(#23) — lives downstream and is intentionally not here.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pyestat._endpoint import ClassObj


class AxisRole(str, Enum):
    """The role one whole axis plays (exactly one per axis)."""

    TIME = "time"
    AREA = "area"
    VALUE = "value"
    CATEGORY = "category"
    META_AXIS = "meta-axis"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    """How many independent signals agreed on a role (Open question 5)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_TIER_ORDER = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


@dataclass(frozen=True)
class AxisClassification:
    """One axis's inferred role, tier, and the evidence behind it."""

    axis_id: str
    role: AxisRole
    confidence: Confidence
    signals: tuple[str, ...]


@dataclass(frozen=True)
class TableClassification:
    """The per-axis classifications for one table, in axis order."""

    axes: tuple[AxisClassification, ...]

    @property
    def role_pattern(self) -> tuple[AxisRole, ...]:
        """Ordered tuple of roles — the key #22 matches rules against."""
        return tuple(a.role for a in self.axes)

    def min_confidence(self) -> Confidence:
        """The weakest axis tier (``high`` for an empty table)."""
        if not self.axes:
            return Confidence.HIGH
        return min((a.confidence for a in self.axes), key=lambda c: _TIER_ORDER[c])

    def clears(self, threshold: Confidence = Confidence.MEDIUM) -> bool:
        """Weakest-link gate: every axis must reach ``threshold``.

        The mechanism only (Open question 5, default ``medium`` — so only
        ``low`` axes route to Layer D). The refinement that *only axes a
        matched rule requires* must clear, plus the actual Layer D
        routing, belong to #22 / #23.
        """
        return _TIER_ORDER[self.min_confidence()] >= _TIER_ORDER[threshold]


# --- signal vocabularies ---------------------------------------------------

# e-Stat date codes are 10-digit (YYYY + 6) or a bare 4-digit year. The
# strict parsers in ``time.py`` reject some real shapes (GDP fiscal-year
# ``1995100000``), so the *role* signal is the digit shape, not a parse.
_DATE_CODE = re.compile(r"^\d{10}$|^\d{4}$")
# JIS X 0401 municipality / prefecture codes are 5 digits. Foreign country
# codes share the shape; both corroborate `area` (vocabulary is #4's job).
_JIS_CODE = re.compile(r"^\d{5}$")

_TIME_NAME_TOKEN = "時間軸"
_AREA_NAME_TOKENS = ("地域", "都道府県", "全国", "市区町村", "国")
_TAB_AXIS_ID = "tab"
_TAB_NAME_TOKEN = "表章項目"
# Deliberately narrow: measure-spread words that mark one logical record
# split across rows. Excludes 指数 / 比 / 性比 etc., which appear in
# ordinary statistical *categories* (see the population mis-pivot trap).
# Retired as the load-bearing meta signal (Open question 7): used only as a
# metadata-only fallback (capped at medium) when data rows are unavailable.
_MEASURE_SPREAD_TOKENS = ("数量", "金額", "単位", "価額")

# Non-numeric cells that carry no unit meaning — e-Stat's suppression /
# not-applicable markers. Excluded before the unit-string test, else a
# sparse page (household is ~90% "-") fakes type heterogeneity.
_MARKER = re.compile(r"^[\s\-\*\.Xx Ｘ…・‐－/]*$")


def _cell_kind(cell: Any) -> str:
    """``'num'`` | ``'str'`` (a genuine unit label) | ``'marker'``."""
    text = str(cell).replace(",", "")
    try:
        float(text)
        return "num"
    except ValueError:
        return "marker" if _MARKER.match(text) else "str"


def _norm(text: str) -> str:
    """NFKC-fold so full/half-width variants compare equal."""
    return unicodedata.normalize("NFKC", text)


def _member_names(axis: ClassObj) -> list[str]:
    return [_norm(str(c.get("name", ""))) for c in axis.classes]


def _member_codes(axis: ClassObj) -> list[str]:
    return [str(c.get("code", "")) for c in axis.classes]


# --- per-role detectors ----------------------------------------------------


def _classify_time(axis: ClassObj) -> AxisClassification | None:
    name = _norm(axis.name)
    id_hit = axis.id == "time"
    name_hit = _TIME_NAME_TOKEN in name
    codes = _member_codes(axis)
    code_hit = bool(codes) and all(_DATE_CODE.match(c) for c in codes)
    if not (id_hit or (name_hit and code_hit)):
        return None
    signals = [s for s, hit in (("id=time", id_hit), ("name=時間軸", name_hit), ("date-shape codes", code_hit)) if hit]
    # Defining signals concur (conventional id + a date-shape/name signal).
    strong = id_hit and (code_hit or name_hit)
    confidence = Confidence.HIGH if strong else Confidence.MEDIUM
    return AxisClassification(axis.id, AxisRole.TIME, confidence, tuple(signals))


def _classify_area(axis: ClassObj) -> AxisClassification | None:
    name = _norm(axis.name)
    id_hit = axis.id == "area"
    name_hit = any(tok in name for tok in _AREA_NAME_TOKENS)
    codes = _member_codes(axis)
    code_hit = bool(codes) and all(_JIS_CODE.match(c) for c in codes)
    if not (id_hit or (name_hit and code_hit)):
        return None
    signals = [s for s, hit in (("id=area", id_hit), ("name token", name_hit), ("jis-shape codes", code_hit)) if hit]
    strong = id_hit and (name_hit or code_hit)
    confidence = Confidence.HIGH if strong else Confidence.MEDIUM
    return AxisClassification(axis.id, AxisRole.AREA, confidence, tuple(signals))


def _classify_tab(axis: ClassObj) -> AxisClassification | None:
    """The 表章項目 axis: single value type → VALUE, many → META_AXIS."""
    if axis.id != _TAB_AXIS_ID and _TAB_NAME_TOKEN not in _norm(axis.name):
        return None
    if len(axis.classes) <= 1:
        return AxisClassification(
            axis.id, AxisRole.VALUE, Confidence.HIGH, ("tab convention, single value type",)
        )
    return AxisClassification(
        axis.id, AxisRole.META_AXIS, Confidence.HIGH, ("tab convention, multiple value types",)
    )


# member code -> [numeric cell count, genuine unit-string cell count].
_CellProfile = Mapping[str, "list[int]"]


def _is_unit_row_meta(profile: _CellProfile) -> bool:
    """A meta-axis signature: a pure unit-string member among numeric ones.

    Vocabulary-free (Open question 7). ``True`` when, over the axis's
    members with informative (non-marker) cells, at least one member is a
    pure unit string (string cells, zero numeric — trade's 単位 → "ＮＯ")
    while at least one other member is numeric (数量 / 金額).
    """
    informative = {m: c for m, c in profile.items() if c[0] + c[1] > 0}
    if len(informative) < 2:
        return False
    has_unit_string = any(num == 0 and string > 0 for num, string in informative.values())
    has_numeric = any(num > 0 for num, _string in informative.values())
    return has_unit_string and has_numeric


def _classify_meta_or_category(
    axis: ClassObj, profile: _CellProfile | None
) -> AxisClassification:
    """Non-tab axis. With data rows, the unit-string signal is authoritative;
    without them, fall back to the (retired) measure-spread lexicon.

    The mis-pivot guard runs throughout: ``@unit`` is never a trigger, and a
    signal that cannot confirm a meta-axis yields ``category`` (data present)
    or ``unknown`` (lexicon-ambiguous) rather than a speculative pivot.
    """
    if profile is not None:
        if _is_unit_row_meta(profile):
            return AxisClassification(
                axis.id, AxisRole.META_AXIS, Confidence.HIGH,
                ("data: unit-string member among numeric members",),
            )
        # Data is present and shows no unit-row split: a plain dimension.
        return AxisClassification(axis.id, AxisRole.CATEGORY, Confidence.MEDIUM, ("data: homogeneous cell types",))

    # Metadata-only fallback: the lexicon, no longer load-bearing, capped at
    # medium (Open question 7).
    name = _norm(axis.name)
    name_hit = any(tok in name for tok in _MEASURE_SPREAD_TOKENS)
    member_hits = sum(
        1 for nm in _member_names(axis)
        if any(tok in nm for tok in _MEASURE_SPREAD_TOKENS)
    )
    if name_hit or member_hits >= 2:
        return AxisClassification(
            axis.id, AxisRole.META_AXIS, Confidence.MEDIUM, ("lexicon fallback (no data)",)
        )
    if member_hits == 1:
        # A lone measure-spread member with no axis-name support: an
        # ambiguous meta candidate. Leave it unknown/low so it routes to
        # Layer D rather than risk a mis-pivot.
        return AxisClassification(
            axis.id, AxisRole.UNKNOWN, Confidence.LOW, ("ambiguous lone measure-spread member",)
        )
    # Category by elimination — a legitimate dimension role, tops at medium.
    return AxisClassification(axis.id, AxisRole.CATEGORY, Confidence.MEDIUM, ("by elimination",))


def _classify_axis(axis: ClassObj, profile: _CellProfile | None) -> AxisClassification:
    for detector in (_classify_time, _classify_area, _classify_tab):
        result = detector(axis)
        if result is not None:
            return result
    return _classify_meta_or_category(axis, profile)


def _cell_profiles(
    rows: Sequence[Mapping[str, Any]], axis_ids: Sequence[str]
) -> dict[str, dict[str, list[int]]]:
    """Per axis, per member code, count numeric vs genuine unit-string cells.

    Rows are Layer 2's flattened form: axis ``@id`` keys (``@`` stripped) and
    the cell under ``value``. Suppression markers are dropped.
    """
    profiles: dict[str, dict[str, list[int]]] = {
        aid: defaultdict(lambda: [0, 0]) for aid in axis_ids
    }
    for row in rows:
        kind = _cell_kind(row.get("value"))
        if kind == "marker":
            continue
        idx = 0 if kind == "num" else 1
        for aid in axis_ids:
            member = row.get(aid)
            if member is not None:
                profiles[aid][member][idx] += 1
    return profiles


def classify(
    class_objs: Sequence[ClassObj],
    table_inf: Mapping[str, Any] | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> TableClassification:
    """Classify every axis of a table into a role + confidence tier.

    ``rows`` are the table's fetched data rows (Layer 2's flattened form).
    When supplied, a non-``tab`` meta-axis is detected from the data via the
    vocabulary-free unit-string signal (Open question 7); when omitted, the
    classifier degrades to metadata-only heuristics.

    ``table_inf`` is accepted for the optional ``TITLE_SPEC``-prefix signal
    the proposal reserves; the MVP heuristics do not yet consult it, but the
    parameter is part of the contract so adding that signal later is not a
    breaking change.
    """
    profiles = (
        _cell_profiles(rows, [c.id for c in class_objs]) if rows is not None else None
    )
    return TableClassification(
        tuple(
            _classify_axis(axis, profiles.get(axis.id) if profiles is not None else None)
            for axis in class_objs
        )
    )
