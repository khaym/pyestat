"""Tests for built-in time parsers (Layer 3).

The parsers are pure functions and cover the three granularities
DESIGN.md ships at MVP. Real e-Stat time codes for the three benchmark
tables are pinned as test cases so a future drift in the wire format
is caught here instead of in a one-off integration run.
"""
from __future__ import annotations

import pytest

from pyestat._time import TimePoint, monthly_e_stat, quarterly_e_stat, yearly


class TestMonthly:
    """Population estimates (0003443838) — 10-digit codes shaped
    ``YYYY00MMMM`` where the trailing pair is the same month repeated."""

    @pytest.mark.parametrize(
        "code,normalized",
        [
            ("2022000101", "2022-01"),
            ("2022000202", "2022-02"),
            ("2022001212", "2022-12"),
            ("2021001212", "2021-12"),  # observed real e-Stat row
        ],
    )
    def test_round_trips_observed_codes(self, code: str, normalized: str) -> None:
        # The codes here came out of an actual e-Stat probe; pinning
        # them stops a future "simplification" from re-introducing the
        # 9-10 vs 7-8 digit confusion DESIGN.md had to call out.
        result = monthly_e_stat(code)
        assert result == TimePoint(normalized, "monthly")

    def test_rejects_a_quarterly_shape(self) -> None:
        # Quarterly codes like ``1994000103`` look superficially similar;
        # if monthly were lenient about start != end it would parse them
        # as January and silently mislabel data.
        with pytest.raises(ValueError):
            monthly_e_stat("1994000103")

    def test_rejects_a_yearly_only_code(self) -> None:
        # ``YYYY000000`` belongs to the yearly parser; surfacing as an
        # exception forces the rule author to choose the right format
        # rather than getting "month 00" silently.
        with pytest.raises(ValueError):
            monthly_e_stat("2026000000")

    @pytest.mark.parametrize("bad", ["", "2022", "2022000099", "abcd000101", "20220001011"])
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            monthly_e_stat(bad)


class TestQuarterly:
    """GDP advance (0003109741) — 10-digit codes shaped
    ``YYYY00<start_mm><end_mm>`` for each quarter span."""

    @pytest.mark.parametrize(
        "code,normalized",
        [
            ("1994000103", "1994-Q1"),
            ("1994000406", "1994-Q2"),
            ("1994000709", "1994-Q3"),
            ("1994001012", "1994-Q4"),
        ],
    )
    def test_maps_each_quarter_span(self, code: str, normalized: str) -> None:
        assert quarterly_e_stat(code) == TimePoint(normalized, "quarterly")

    def test_rejects_an_arbitrary_month_span(self) -> None:
        # 0205 (Feb-May) is not a recognized quarter; the parser must
        # refuse rather than invent a "Q-half-1" label.
        with pytest.raises(ValueError):
            quarterly_e_stat("2022000205")

    def test_rejects_a_monthly_code(self) -> None:
        with pytest.raises(ValueError):
            quarterly_e_stat("2022000101")


class TestYearly:
    """Trade (0004049306) — yearly codes either bare ``YYYY`` or the
    10-digit ``YYYY000000`` form e-Stat actually returned for that table."""

    @pytest.mark.parametrize("code", ["2020", "2026", "2026000000", "1994000000"])
    def test_accepts_short_and_long_form(self, code: str) -> None:
        result = yearly(code)
        assert result.granularity == "yearly"
        assert result.normalized == code[:4]

    @pytest.mark.parametrize("bad", ["", "20", "abc", "2022001212", "2022000101"])
    def test_rejects_non_yearly(self, bad: str) -> None:
        with pytest.raises(ValueError):
            yearly(bad)


class TestTimePointShape:
    def test_is_immutable(self) -> None:
        tp = TimePoint("2020-01", "monthly")
        with pytest.raises(Exception):
            tp.normalized = "x"  # type: ignore[misc]
