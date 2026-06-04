"""Rule resolution for the v2 auto path (Layers C > B > A, #28).

Given a table's axis classification, pick the rule to apply:

* **C / B** — the highest-precedence v2 rule whose ``match.role_pattern``
  equals the table's classified role pattern. Layers are walked in
  resolution order user (C) > project (C) > builtin (B); the first layer
  with a match wins, and two rules matching in the *same* layer are an
  authoring conflict surfaced as :class:`AmbiguousRuleError`.
* **A** — when no specific rule matched, a generic rule built from the
  role-default registry (:func:`build_generic_rule`), which itself returns
  ``None`` for tables that need a pivot.

Returning ``None`` means "route to Layer D": either the classification is
too weak to trust (its weakest axis is below ``threshold``, so the role
pattern itself is unreliable) or no rule — specific or generic — could be
produced. The endpoint owns the actual Layer D call, so this module stays
free of request plumbing and is unit-testable from a hand-built
classification.
"""
from __future__ import annotations

from collections.abc import Sequence

from pyestat._engine.classifier import Confidence, TableClassification
from pyestat._engine.role_defaults import build_generic_rule
from pyestat._engine.rule import RuleV2
from pyestat.errors import AmbiguousRuleError


def resolve_v2(
    classification: TableClassification,
    *,
    user: Sequence[RuleV2] = (),
    project: Sequence[RuleV2] = (),
    builtin: Sequence[RuleV2] = (),
    threshold: Confidence = Confidence.MEDIUM,
    stats_data_id: str = "",
) -> RuleV2 | None:
    """Resolve the rule for a classified table, or ``None`` for Layer D.

    See the module docstring for the resolution order and the meaning of a
    ``None`` return. ``stats_data_id`` only labels an
    :class:`AmbiguousRuleError`.
    """
    if not classification.clears(threshold):
        return None
    pattern = list(classification.role_pattern)
    for layer in (user, project, builtin):
        matched = [rule for rule in layer if list(rule.match.role_pattern) == pattern]
        if len(matched) > 1:
            raise AmbiguousRuleError(stats_data_id=stats_data_id, matched_rules=matched)
        if matched:
            return matched[0]
    return build_generic_rule(classification)
