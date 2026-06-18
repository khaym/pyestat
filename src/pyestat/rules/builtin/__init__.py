"""Library-bundled rules for representative e-Stat tables.

Each YAML in this directory ships with pyestat and is loaded by
:func:`pyestat.load_builtin_rules`. New rules are added by dropping
another ``*.yaml`` in here that conforms to the schema in
``pyestat._engine.rule.RuleV2``.

#30 retired the never-published v1 rules. #29 ships the one benchmark
table Layer A cannot fold on its own — foreign trade's hierarchical
measure×period cross — as two rules, one per structural group of the
trade family (``foreign_trade.yaml`` = 品別国別表, ``foreign_trade_customs.yaml``
= 税関別). GDP and the population estimates structure generically via
Layer A, so they need no bundled rule.
"""
