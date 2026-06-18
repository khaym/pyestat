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

A non-``None`` result is a :class:`ResolvedRule` pairing the rule with the
*layer* it came from. The layer is the provenance the auto path needs to
decide, when applying the rule fails, whether to surface the failure (a
rule the caller authored) or degrade to Layer D (a library-provided rule);
see ``docs/DESIGN.md`` Decision B.
"""
from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import NamedTuple

from pyestat._endpoint import ClassObj
from pyestat._engine.classifier import Confidence, TableClassification
from pyestat._engine.role_defaults import build_generic_rule
from pyestat._engine.rule import RuleV2
from pyestat.errors import AmbiguousRuleError


class RuleLayer(Enum):
    """Which resolution layer produced a rule (Decision E).

    The distinction the auto path acts on is *caller-authored* (user /
    project) versus *library-provided* (builtin / generic) — see
    :attr:`is_caller_authored`. The four values are kept distinct so
    diagnostics and future policy can tell the layers apart.
    """

    USER = "user"
    PROJECT = "project"
    BUILTIN = "builtin"
    GENERIC = "generic"

    @property
    def is_caller_authored(self) -> bool:
        """True for rules the caller passed or wrote (user / project), whose
        application failures surface; False for library-provided rules
        (builtin / generic), whose failures degrade to Layer D."""
        return self in (RuleLayer.USER, RuleLayer.PROJECT)


class ResolvedRule(NamedTuple):
    """A resolved rule and the layer it was resolved from."""

    rule: RuleV2
    layer: RuleLayer


def resolve_v2(
    classification: TableClassification,
    *,
    user: Sequence[RuleV2] = (),
    project: Sequence[RuleV2] = (),
    builtin: Sequence[RuleV2] = (),
    class_objs: Sequence[ClassObj] = (),
    threshold: Confidence = Confidence.MEDIUM,
    stats_data_id: str = "",
    stats_code: str | None = None,
) -> ResolvedRule | None:
    """Resolve the rule for a classified table, or ``None`` for Layer D.

    See the module docstring for the resolution order, the provenance the
    :class:`ResolvedRule` layer carries, and the meaning of a ``None``
    return. ``stats_data_id`` only labels an :class:`AmbiguousRuleError`.

    ``stats_code`` is the table's e-Stat survey-family code (from
    ``TABLE_INF.STAT_NAME.@code``). A rule that pins ``match.stats_code``
    matches only when it equals this; a rule that leaves it unset matches by
    role pattern alone (#29). A table without a statsCode (``None``) matches
    only unscoped rules, so a family-specific rule never guesses.

    ``class_objs`` carries the table's class metadata; the generic Layer A
    fallback needs the meta-axis member names to auto-generate a pivot rule
    (#34). Omitted (the default), a meta-axis table cannot be pivoted and
    routes to Layer D.
    """
    if not classification.clears(threshold):
        return None
    pattern = list(classification.role_pattern)

    def _matches(rule: RuleV2) -> bool:
        if list(rule.match.role_pattern) != pattern:
            return False
        # stats_code is an extra AND-narrowing: an unset rule applies to any
        # family; a set rule needs the table's family to confirm it (#29).
        return rule.match.stats_code is None or rule.match.stats_code == stats_code

    for rules, layer in (
        (user, RuleLayer.USER),
        (project, RuleLayer.PROJECT),
        (builtin, RuleLayer.BUILTIN),
    ):
        matched = [rule for rule in rules if _matches(rule)]
        if len(matched) > 1:
            # A same-layer conflict follows the surface/degrade policy by
            # provenance (DESIGN.md Decision B): a caller-authored layer
            # (user / project) surfaces so the caller can fix their rules; a
            # library layer (builtin) is a packaging bug the caller cannot
            # fix, so skip it and fall through to a generic rule or Layer D
            # rather than crash. (Built-in conflicts are meant to be caught
            # in CI; degrading keeps a release-time slip from breaking calls.)
            if layer.is_caller_authored:
                raise AmbiguousRuleError(
                    stats_data_id=stats_data_id, matched_rules=matched
                )
            continue
        if matched:
            return ResolvedRule(matched[0], layer)
    generic = build_generic_rule(classification, class_objs)
    if generic is None:
        return None
    return ResolvedRule(generic, RuleLayer.GENERIC)
