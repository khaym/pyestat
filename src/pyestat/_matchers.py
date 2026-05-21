"""Matcher pipeline (Layer 3 sub-component).

Each :class:`Matcher` returns ``True`` when its rule applies to the
response. The :class:`RuleManager` ANDs every Matcher together — a
rule matches only if *all* Matchers say yes for the same
``(response, rule)`` pair. Appending a new Matcher (e.g. an exact
``statsDataId`` check) is a one-liner that does not touch existing
Matchers (ARCHITECTURE.md, extension points table).
"""
from __future__ import annotations

from typing import Protocol

from pyestat._endpoint import StatsDataResponse
from pyestat._fingerprint import Fingerprint
from pyestat._rule import Rule


class Matcher(Protocol):
    """Boolean predicate over a (response, rule) pair."""

    def matches(self, response: StatsDataResponse, rule: Rule) -> bool: ...


class StatsCodeMatcher:
    """Narrows by the e-Stat statistic-family code.

    ``statsCode`` lives in ``TABLE_INF.STAT_NAME.@code`` on the
    response side. ``TABLE_INF`` schema drift means ``STAT_NAME`` is
    sometimes a bare string instead of a ``{@code, $}`` dict; in that
    case the @code is inaccessible and the matcher reports no match
    rather than crashing.
    """

    def matches(self, response: StatsDataResponse, rule: Rule) -> bool:
        stat_name = response.table_inf.get("STAT_NAME")
        if not isinstance(stat_name, dict):
            return False
        return stat_name.get("@code") == rule.match.statsCode


class FingerprintMatcher:
    """Refuses rules whose named axes are absent from the response.

    DESIGN.md Decision A specifies a name-digest component in addition
    to the axis-id set; at MVP only the axis-id set is consulted
    because rule files do not yet carry a name-digest claim. The
    :class:`pyestat._fingerprint.Fingerprint` instance is built up
    front so a future "rule carries an expected digest" extension
    plugs in without reshaping this Matcher.
    """

    def matches(self, response: StatsDataResponse, rule: Rule) -> bool:
        expected: set[str] = {rule.axes.time.id}
        if rule.axes.area is not None:
            expected.add(rule.axes.area.id)
        fingerprint = Fingerprint.from_response(response)
        return expected.issubset(fingerprint.axis_ids)
