"""Tests for the library-bundled rules.

#30 retired the never-published v1 built-in rules; #29 rewrites the
three benchmark tables (population estimates, quarterly GDP, foreign
trade) in v2. Until then the bundle ships no rules, so these tests pin
only the loader *contract* — every bundled rule is a :class:`RuleV2`.
The "three benchmark tables are covered" assertion returns with #29.
"""
from __future__ import annotations

from pyestat import RuleV2, load_builtin_rules


class TestBuiltinRuleContract:
    def test_all_bundled_rules_are_v2(self) -> None:
        # The auto path resolves by role pattern and considers only v2
        # rules; a stray non-v2 rule in the bundle would silently never
        # fire. Pinning the type keeps the bundle honest as #29 adds rules.
        assert all(isinstance(r, RuleV2) for r in load_builtin_rules())

    def test_bundle_is_currently_empty(self) -> None:
        # Documents the post-#30 / pre-#29 state explicitly: no v1 remnant
        # is left loading. This flips to a coverage assertion in #29.
        assert load_builtin_rules() == []
