"""Built-in time parsers shipped at MVP (Layer 3 helper).

Each parser maps an e-Stat time-axis code into a normalized string
plus a granularity tag. Parsers are pure functions so the registry
can hand them around without instantiation overhead.

Output convention favors ISO 8601 where it applies (``YYYY-MM``,
``YYYY``) and falls back to the widely-recognized ``YYYY-Qn`` notation
for quarters, which have no ISO 8601 form. The granularity tag is a
stable enum-like string so a caller can roll monthly into yearly
without re-parsing.

Observed wire shapes (pinned in ``tests/test_time.py``):

* Population estimates: 10-digit ``YYYY00MMMM`` (trailing pair is the
  same month, repeated — DESIGN.md initially mis-described this as
  five-digit).
* Quarterly GDP: 10-digit ``YYYY00<start_mm><end_mm>``.
* Trade (yearly): 10-digit ``YYYY000000``; the bare four-digit form
  also accepted to keep custom rules ergonomic.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from pyestat._registry import Registry


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
    and ``YYYY000000`` (what real e-Stat yearly tables return).
    """
    if re.fullmatch(r"\d{4}", code):
        return TimePoint(code, "yearly")
    if re.fullmatch(r"\d{4}000000", code):
        return TimePoint(code[:4], "yearly")
    raise ValueError(f"not a yearly time code: {code!r}")


TimeParser = Callable[[str], TimePoint]

# Pre-populated registry of built-in time parsers. Custom parsers can
# be added at import time by user code via ``TIME_PARSERS.register``.
TIME_PARSERS: Registry[TimeParser] = Registry(kind="time parser")
TIME_PARSERS.register("monthly_e_stat", monthly_e_stat)
TIME_PARSERS.register("quarterly_e_stat", quarterly_e_stat)
TIME_PARSERS.register("yearly", yearly)
