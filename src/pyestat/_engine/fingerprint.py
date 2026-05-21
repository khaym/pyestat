"""Structural fingerprint of a ``StatsDataResponse`` (Layer 3 helper).

The fingerprint compresses two things about a table:

* the *set* of axis ``@id`` values it carries, and
* a stable digest of the axis names after normalization that collapses
  the drift observed across tables (full-width vs half-width parens,
  trailing parenthesized qualifiers around a shared stem).

DESIGN.md Decision A specifies a hybrid matching strategy where the
fingerprint validates that a candidate rule still applies. The MVP
fingerprint matcher consults only the axis-id set; the name digest
is computed up front so a future "rule carries a name-digest claim"
extension can land without reshaping the type.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from pyestat._endpoint import StatsDataResponse


@dataclass(frozen=True)
class Fingerprint:
    axis_ids: frozenset[str]
    name_digest: str

    @classmethod
    def from_response(cls, response: StatsDataResponse) -> "Fingerprint":
        pairs = {
            obj.id: _normalize_axis_name(obj.name) for obj in response.class_objs
        }
        canonical = "|".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        return cls(
            axis_ids=frozenset(pairs),
            name_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_axis_name(name: str) -> str:
    """Collapse the axis-name drift observed across e-Stat tables.

    NFKC unifies the full-width vs half-width parenthesis variants
    ("時間軸（年次）" vs "時間軸(年次)"); the trailing parenthesized
    qualifier ("（年月日現在）", "（四半期）") is stripped because it
    describes the granularity rather than the axis identity; and the
    result is case-folded for stable comparison.
    """
    return _TRAILING_PAREN.sub("", unicodedata.normalize("NFKC", name)).casefold()
