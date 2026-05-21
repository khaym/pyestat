"""Library-bundled rules for representative e-Stat tables.

Each YAML in this directory ships with pyestat and is loaded by
:func:`pyestat.load_builtin_rules`. New rules are added by dropping
another ``*.yaml`` in here that conforms to the schema in
``pyestat._engine.rule.Rule``.
"""
