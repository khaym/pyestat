"""Library-bundled rule loader.

Reads every ``*.yaml`` under :mod:`pyestat.rules.builtin` into
:class:`RuleV2` instances. Uses ``importlib.resources`` so the loader
works when pyestat is installed from a wheel (where the YAML files
sit inside ``site-packages``) as well as from a working tree.

The never-published v1 built-in rules were retired; the bundle now
ships the one benchmark table Layer A cannot fold on its own — foreign trade's
hierarchical measure×period cross — as two rules, one per structural
group of the trade family (品別国別表 and 税関別). GDP and the population
estimates structure generically via Layer A, so the bundle carries only
trade.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path

from pyestat._engine.rule import RuleV2
from pyestat._engine.loader import YamlRuleLoader


def load_builtin_rules() -> list[RuleV2]:
    """Return every rule shipped under ``pyestat/rules/builtin/``.

    Order is by filename so a future ``AmbiguousRuleError`` would list
    candidate rules in a stable, diff-friendly order.
    """
    loader = YamlRuleLoader()
    root = resources.files("pyestat.rules.builtin")
    rules: list[RuleV2] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".yaml"):
            # importlib.resources files have a ``read_text`` API but
            # YamlRuleLoader expects a path. ``as_file`` gives us a
            # concrete path even when the package is shipped from a
            # zip-style wheel.
            with resources.as_file(entry) as path:
                rules.append(loader.load(Path(path)))
    return rules
