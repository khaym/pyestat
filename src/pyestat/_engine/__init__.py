"""Layer 3 — the rule-driven transformation engine.

Submodules form a small DAG:

* :mod:`pyestat._engine.registry` — name → impl lookup primitive.
* :mod:`pyestat._engine.time` — built-in time parsers + ``best_effort``.
* :mod:`pyestat._engine.rule` — RuleV2 output-schema pydantic model.
* :mod:`pyestat._engine.loader` — YAML loader for the schema.
* :mod:`pyestat._engine.classifier` — axis classifier (role + confidence; Layer A).
* :mod:`pyestat._engine.role_defaults` — role-default registry + short-form expansion.
* :mod:`pyestat._engine.resolver` — v2 rule resolution (Layers C > B > A).
* :mod:`pyestat._engine.apply` — glue that runs the resolved rule over rows.
* :mod:`pyestat._engine.builtin` — loader for library-bundled rules.

Public symbols (``EstatClient``, ``RuleV2``, ``load_builtin_rules`` …)
re-export from :mod:`pyestat`. Direct ``pyestat._engine.X`` imports are
internal.
"""
