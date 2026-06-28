"""Built-in time parsers (Layer 3 helper).

Each parser maps an e-Stat time-axis code into a normalized string
plus a granularity tag. Parsers are pure functions, imported and called
directly; the v2 apply path wraps them as named transforms in
``role_defaults.TRANSFORMS`` (the single transform registry).

Output convention favors ISO 8601 where it applies (``YYYY-MM``,
``YYYY``) and falls back to the widely-recognized ``YYYY-Qn`` notation
for quarters, which have no ISO 8601 form. The granularity tag is a
stable enum-like string so a caller can roll monthly into yearly
without re-parsing.

Observed wire shapes (pinned in ``tests/test_time.py``):

* Population estimates: 10-digit ``YYYY00MMMM`` (trailing pair is the
  same month, repeated).
* Quarterly GDP: 10-digit ``YYYY00<start_mm><end_mm>``.
* Trade (yearly): 10-digit ``YYYY000000``; the bare four-digit form
  also accepted to keep custom rules ergonomic.
* Fiscal year (GDP annual, CPI): 10-digit ``YYYY100000`` — 「YYYY年度」,
  normalized as the April-start span ``YYYY-04``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TimePoint:
    """One normalized time-axis value.

    ``normalized`` is the display-friendly / ISO 8601-leaning form;
    ``granularity`` is the stable tag that downstream aggregation
    (caller-side; out of pyestat scope) keys off.
    """

    normalized: str
    granularity: str


_DIGITS_10 = re.compile(r"^\d{10}$")
_QUARTER_END_MAP: dict[tuple[str, str], int] = {
    ("01", "03"): 1,
    ("04", "06"): 2,
    ("07", "09"): 3,
    ("10", "12"): 4,
}


def monthly_e_stat(code: str) -> TimePoint:
    """Parse a 10-digit monthly e-Stat code into ``YYYY-MM`` / monthly.

    Raises ``ValueError`` for any shape that is not a monthly code — in
    particular quarterly and yearly inputs are rejected here so the
    rule author is forced to pick the right ``format`` rather than get
    nonsense like ``"2026-00"``.
    """
    if not _DIGITS_10.match(code):
        raise ValueError(f"not a 10-digit time code: {code!r}")
    if code[4:6] != "00":
        raise ValueError(f"unexpected separator in monthly code: {code!r}")
    start, end = code[6:8], code[8:10]
    if start != end:
        raise ValueError(
            f"monthly code must repeat the month (got {start}-{end}): {code!r}"
        )
    month = int(start)
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range in {code!r}")
    year = code[:4]
    return TimePoint(f"{year}-{month:02d}", "monthly")


def quarterly_e_stat(code: str) -> TimePoint:
    """Parse a 10-digit quarterly e-Stat code into ``YYYY-Qn`` / quarterly."""
    if not _DIGITS_10.match(code):
        raise ValueError(f"not a 10-digit time code: {code!r}")
    if code[4:6] != "00":
        raise ValueError(f"unexpected separator in quarterly code: {code!r}")
    start, end = code[6:8], code[8:10]
    quarter = _QUARTER_END_MAP.get((start, end))
    if quarter is None:
        raise ValueError(f"not a recognized quarter span: {code!r}")
    return TimePoint(f"{code[:4]}-Q{quarter}", "quarterly")


def yearly(code: str) -> TimePoint:
    """Parse a yearly code into ``YYYY`` / yearly.

    Accepts both ``YYYY`` (the form a hand-authored rule would use)
    and ``YYYY000000`` (what real e-Stat yearly tables return). The
    fiscal-year shape ``YYYY100000`` is *rejected* here — it belongs to
    :func:`fiscal_year_e_stat` — so a rule that declared ``yearly`` on
    fiscal data fails loudly instead of relabeling an Apr–Mar aggregate
    as the calendar year.
    """
    if re.fullmatch(r"\d{4}", code):
        return TimePoint(code, "yearly")
    if re.fullmatch(r"\d{4}000000", code):
        return TimePoint(code[:4], "yearly")
    raise ValueError(f"not a yearly time code: {code!r}")


def fiscal_year_e_stat(code: str) -> TimePoint:
    """Parse the fiscal-year wire shape ``YYYY100000`` into ``YYYY-04`` /
    yearly.

    e-Stat encodes 「1995年度」 as ``1995100000`` — the ``10`` at the
    separator position is a fiscal *flag*, not a month. The normalized form
    is the April-start year span: yearly granularity so year rollups
    work, with the start month as the period identity — the same vocabulary
    as a range-named span (population's Oct-start year is ``2006-10``) — so
    calendar 「2015年」 and fiscal 「2015年度」, which CPI ships side by
    side in one time axis, never merge into one ``"2015"`` bucket.

    The April start is Japan's statutory fiscal year, a convention the code
    itself does not carry: in the 2026-06-09 survey every ``YYYY100000``
    member is named 「YYYY年度」 or 「YYYY年度末」; a non-April 年度 family
    (米穀年度 etc.) would need its own handling if one ever surfaces.
    """
    if re.fullmatch(r"\d{4}100000", code):
        return TimePoint(f"{code[:4]}-04", "yearly")
    raise ValueError(f"not a fiscal-year time code: {code!r}")


# A full month-to-month range in a time member's display name
# (「2006年10月～2007年9月」). The separator is FULLWIDTH TILDE (U+FF5E,
# what e-Stat ships), WAVE DASH (U+301C, its classic encoding drift), or
# ASCII tilde. Anchored: in the 2026-06-10 survey (11,106 tables) all 212
# range-named monthly-shaped members match this pattern exactly.
_YEAR_SPAN_NAME = re.compile(r"(\d{4})年(\d{1,2})月[～〜~](\d{4})年(\d{1,2})月")


def _span_months(name: str) -> int | None:
    """The inclusive month count of a full-range member name, or ``None``
    when the name is not a 「N年N月～N年N月」 range."""
    m = _YEAR_SPAN_NAME.fullmatch(name)
    if m is None:
        return None
    y1, m1, y2, m2 = map(int, m.groups())
    return (y2 - y1) * 12 + (m2 - m1) + 1


def best_effort(code: str, name: str | None = None) -> TimePoint | None:
    """Probe the built-in parsers specific→general; first match or ``None``.

    A *total* function: it never raises. Each parser rejects shapes it
    does not own with ``ValueError``, so a code none recognise yields
    ``None`` and the caller keeps the raw value. Shared by Layer D's
    no-rule fallback and Layer A's ``time`` role-default so
    both agree on what counts as a recognisable time code.

    ``name`` is the member's display name, the only signal that separates a
    *year span* from a month: population vital statistics encode an
    October-start annual aggregate (「2006年10月～2007年9月」) as
    ``2006001010`` — byte-identical to monthly 2006-10. The demotion is
    verified, not marker-sniffed: a **monthly** parse becomes yearly
    granularity (normalized keeps the start month) only when the name is a
    full 「N年N月～N年N月」 range spanning exactly 12 months; a range of any
    other length is unrecognised (``None`` — honestly raw beats a wrong
    granularity), and a name that merely contains a tilde stays monthly.
    Only the monthly parse is ever demoted: quarterly member names carry ～
    too (GDP's 「1994年1～3月期」), but a quarterly code is shape-distinct
    (start ≠ end months) and never reaches the monthly parser.
    """
    for parser in (monthly_e_stat, quarterly_e_stat, yearly, fiscal_year_e_stat):
        try:
            point = parser(code)
        except ValueError:
            continue
        if parser is monthly_e_stat and name is not None:
            span = _span_months(name)
            if span == 12:
                return TimePoint(point.normalized, "yearly")
            if span is not None:
                return None
        return point
    return None
