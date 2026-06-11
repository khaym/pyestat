"""Tests for built-in time parsers (Layer 3).

The parsers are pure functions and cover the three granularities
DESIGN.md ships at MVP. Real e-Stat time codes for the three benchmark
tables are pinned as test cases so a future drift in the wire format
is caught here instead of in a one-off integration run.
"""
from __future__ import annotations

import pytest

from pyestat._engine.time import (
    TimePoint,
    best_effort,
    fiscal_year_e_stat,
    monthly_e_stat,
    quarterly_e_stat,
    yearly,
)


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

    @pytest.mark.parametrize("bad", ["", "20", "abc", "2022001212", "2022000101", "1995100000"])
    def test_rejects_non_yearly(self, bad: str) -> None:
        # 1995100000 is the fiscal-year shape (its own parser); a rule that
        # declared `yearly` on fiscal data should hear about it loudly
        # rather than have an Apr–Mar aggregate silently relabeled 暦年.
        with pytest.raises(ValueError):
            yearly(bad)


class TestFiscalYear:
    """CPI / GDP annual tables encode 「1995年度」 as ``YYYY100000``. A
    fiscal year is normalized as the April-start year span ``YYYY-04`` with
    yearly granularity (#33 decision): one vocabulary for every non-calendar
    12-month aggregate (population's Oct-start span is ``2006-10`` the same
    way), and 「2015年」 / 「2015年度」 — which CPI ships side by side in one
    time axis — stay distinguishable instead of double-counting under "2015".

    The April start is a convention (Japan's statutory fiscal year), not in
    the wire code: across the 2026-06-09 survey every YYYY100000 member is
    named 「YYYY年度」(54,740) or 「YYYY年度末」(33) — no non-April 年度
    (米穀年度 etc.) uses this shape.
    """

    def test_fiscal_year_is_the_april_start_span(self) -> None:
        assert fiscal_year_e_stat("1995100000") == TimePoint("1995-04", "yearly")

    def test_calendar_and_fiscal_members_of_one_axis_stay_distinct(self) -> None:
        # CPI 0003036792's time axis carries 「2015年」(2015000000) and
        # 「2015年度」(2015100000) as siblings; a caller grouping yearly rows
        # by `normalized` must see two buckets, not a silent merge.
        calendar = best_effort("2015000000")
        fiscal = best_effort("2015100000")
        assert calendar == TimePoint("2015", "yearly")
        assert fiscal == TimePoint("2015-04", "yearly")
        assert calendar.normalized != fiscal.normalized

    @pytest.mark.parametrize("bad", ["", "1995", "1995000000", "1995200000", "1995101010"])
    def test_rejects_non_fiscal_shapes(self, bad: str) -> None:
        # 1995200000 pins the claim's boundary: only the observed 10-flag
        # separator is fiscal; an unobserved separator stays unrecognised.
        with pytest.raises(ValueError):
            fiscal_year_e_stat(bad)


class TestBestEffortSpanDisambiguation:
    """``best_effort`` with the member's display name (#33).

    The year-span codes of population vital statistics (0003001309,
    「2006年10月～2007年9月」 = an October-start annual aggregate) are
    byte-identical to a monthly code (``2006001010`` = 2006-10), so the code
    alone cannot tell them apart. The member name is the only disambiguation
    signal, and it is *verified*, not pattern-sniffed: a monthly parse is
    demoted to yearly granularity (normalized stays the start month) only
    when the name is a full 「N年N月～N年N月」 range spanning exactly 12
    months. A range of any other length is honestly unrecognised (``None``)
    rather than mislabeled; a name that merely contains a tilde is left
    monthly. Evidence (2026-06-10 survey, 11,106 tables): every one of the
    212 range-named monthly-shaped members matches the full pattern and
    spans exactly 12 months.

    Quarterly member names also contain ～ (GDP's 「1994年1～3月期」), but a
    quarterly code is shape-distinct (start ≠ end months), so only the
    monthly parse is ever demoted.
    """

    def test_monthly_shaped_code_with_year_range_name_is_a_year_span(self) -> None:
        point = best_effort("2006001010", "2006年10月～2007年9月")
        assert point == TimePoint("2006-10", "yearly")

    def test_wave_dash_range_marker_also_counts(self) -> None:
        # The observed marker is FULLWIDTH TILDE (U+FF5E); WAVE DASH
        # (U+301C) is the classic encoding drift of the same character.
        point = best_effort("2006001010", "2006年10月〜2007年9月")
        assert point == TimePoint("2006-10", "yearly")

    def test_sub_year_range_is_unrecognised_not_yearly(self) -> None:
        # A 6-month aggregate must not be tagged yearly — a caller rolling
        # up granularity=="yearly" would double-count two half-years in one
        # year. Unrecognised (raw) is the honest degradation.
        assert best_effort("2006000404", "2006年4月～2006年9月") is None

    def test_multi_year_range_is_unrecognised_not_yearly(self) -> None:
        assert best_effort("2006001010", "2006年10月～2008年9月") is None

    def test_decorative_tilde_does_not_demote(self) -> None:
        # A tilde that is not part of a full N年N月～N年N月 range (annotation,
        # open-ended note, transcoding artifact) leaves the monthly parse
        # untouched — presence of the marker alone is not evidence of a span.
        assert best_effort("2020000101", "2020年1月（～速報～）") == TimePoint("2020-01", "monthly")

    def test_plain_month_name_stays_monthly(self) -> None:
        assert best_effort("2024001212", "2024年12月") == TimePoint("2024-12", "monthly")

    def test_no_name_stays_monthly(self) -> None:
        # Callers without class metadata (a hand-built row) keep today's
        # behavior; the name is an optional, additive signal.
        assert best_effort("2006001010") == TimePoint("2006-10", "monthly")

    def test_quarterly_code_with_range_name_is_not_demoted(self) -> None:
        # GDP quarter names carry ～ too; demoting them would mislabel
        # every quarterly table. The quarter shape wins.
        point = best_effort("1994000103", "1994年1～3月期")
        assert point == TimePoint("1994-Q1", "quarterly")

    def test_fiscal_year_code_resolves_to_april_start_span(self) -> None:
        assert best_effort("1995100000", "1995年度") == TimePoint("1995-04", "yearly")

    def test_unrecognised_code_stays_none_even_with_range_name(self) -> None:
        assert best_effort("1995300000", "なにか～なにか") is None


class TestTimePointShape:
    def test_is_immutable(self) -> None:
        tp = TimePoint("2020-01", "monthly")
        with pytest.raises(Exception):
            tp.normalized = "x"  # type: ignore[misc]
