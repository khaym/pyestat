"""Tests for the structural fingerprint (Layer 3 helper).

The fingerprint collapses axis-name drift across e-Stat tables. Real
drifts observed during the design phase — full-width vs half-width
parentheses around the same stem — are pinned here so a future
refactor cannot accidentally re-introduce the false-mismatch problem.
"""
from __future__ import annotations

from pyestat._endpoint import ClassObj, StatsDataResponse
from pyestat._fingerprint import Fingerprint


def _resp(*pairs: tuple[str, str]) -> StatsDataResponse:
    """Build a minimal StatsDataResponse from (axis_id, axis_name) pairs."""
    return StatsDataResponse(
        stats_data_id="X",
        total_number=None,
        table_inf={},
        class_objs=tuple(ClassObj(id=i, name=n, classes=()) for i, n in pairs),
        values=(),
    )


class TestAxisIdsSet:
    def test_captures_axis_ids_as_a_set(self) -> None:
        # @ids are the stable handle. The order in which e-Stat returns
        # them is not guaranteed, so the fingerprint is order-insensitive.
        fp = Fingerprint.from_response(_resp(("tab", "T"), ("cat01", "C"), ("time", "X")))
        assert fp.axis_ids == frozenset({"tab", "cat01", "time"})


class TestNameDigestStability:
    """The digest must equate axis-name strings that *mean the same thing*
    after normalization, and differ otherwise. These cases come from the
    real drift observed across the three benchmark tables."""

    def test_full_width_and_half_width_parens_collapse(self) -> None:
        # Both "時間軸（年次）" (full-width) and "時間軸(年次)" (half-width)
        # appeared in the three-table probe. NFKC must fold them.
        a = Fingerprint.from_response(_resp(("time", "時間軸（年次）")))
        b = Fingerprint.from_response(_resp(("time", "時間軸(年次)")))
        assert a.name_digest == b.name_digest

    def test_trailing_parenthesized_qualifier_dropped(self) -> None:
        # "時間軸（年月日現在）" (monthly) and "時間軸（四半期）" (quarterly)
        # share the same stem "時間軸"; the qualifier is metadata about
        # the granularity, not the axis identity.
        a = Fingerprint.from_response(_resp(("time", "時間軸（年月日現在）")))
        b = Fingerprint.from_response(_resp(("time", "時間軸（四半期）")))
        assert a.name_digest == b.name_digest

    def test_distinct_stems_produce_distinct_digests(self) -> None:
        # The fold must not be so aggressive it makes every axis look
        # alike — "表章項目" and "時間軸" are conceptually different
        # and must stay distinguishable.
        a = Fingerprint.from_response(_resp(("time", "時間軸（年次）")))
        b = Fingerprint.from_response(_resp(("time", "表章項目")))
        assert a.name_digest != b.name_digest

    def test_digest_is_order_independent(self) -> None:
        # The canonical form sorts by axis @id, so the same {id → name}
        # mapping produces the same digest regardless of CLASS_OBJ order.
        a = Fingerprint.from_response(_resp(("tab", "表章項目"), ("time", "時間軸")))
        b = Fingerprint.from_response(_resp(("time", "時間軸"), ("tab", "表章項目")))
        assert a.name_digest == b.name_digest
