"""Rule selection orchestrator (Layer 3).

Walks the candidate rule list through the matcher pipeline, respecting
the three-layer resolution order (Decision E):

    user > project > builtin

Earlier layers shadow later ones — but only when the earlier layer
actually *matches*; an unrelated user rule must not block a builtin
rule from firing on a different table.
"""
from __future__ import annotations

from collections.abc import Sequence

from pyestat._endpoint import StatsDataResponse
from pyestat._engine.matchers import FingerprintMatcher, Matcher, StatsCodeMatcher
from pyestat._engine.rule import Rule
from pyestat.errors import AmbiguousRuleError


DEFAULT_MATCHER_PIPELINE: tuple[Matcher, ...] = (
    StatsCodeMatcher(),
    FingerprintMatcher(),
)


class RuleManager:
    """Selects the applicable rule for a given response.

    Returning ``None`` is a documented outcome — the caller falls back
    to Decision B's raw mode in that case.
    """

    def __init__(
        self,
        *,
        user: Sequence[Rule] | None = None,
        project: Sequence[Rule] | None = None,
        builtin: Sequence[Rule] | None = None,
        pipeline: Sequence[Matcher] | None = None,
    ) -> None:
        # Stored in resolution order so iteration is straightforward.
        # An absent layer is treated the same as an empty list.
        self._layers: list[list[Rule]] = [
            list(user or ()),
            list(project or ()),
            list(builtin or ()),
        ]
        self._pipeline = tuple(pipeline) if pipeline is not None else DEFAULT_MATCHER_PIPELINE

    def select(self, response: StatsDataResponse) -> Rule | None:
        for layer in self._layers:
            matched = [rule for rule in layer if self._all_match(response, rule)]
            if len(matched) > 1:
                raise AmbiguousRuleError(
                    stats_data_id=response.stats_data_id, matched_rules=matched
                )
            if matched:
                return matched[0]
        return None

    def _all_match(self, response: StatsDataResponse, rule: Rule) -> bool:
        return all(m.matches(response, rule) for m in self._pipeline)
