"""Library-bundled rules for representative e-Stat tables.

Each YAML in this directory ships with pyestat and is loaded by
:func:`pyestat.load_builtin_rules`. New rules are added by dropping
another ``*.yaml`` in here that conforms to the schema in
``pyestat._engine.rule.RuleV2``.

#30 retired the never-published v1 rules; #29 repopulates this directory
with the three benchmark tables in v2.
"""
