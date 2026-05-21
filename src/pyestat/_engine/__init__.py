"""Layer 3 — the rule-driven transformation engine.

Submodules form a small DAG:

* :mod:`pyestat._engine.registry` — name → impl lookup primitive.
* :mod:`pyestat._engine.time` — built-in time parsers + ``TIME_PARSERS``.
* :mod:`pyestat._engine.rule` — Rule pydantic schema.
* :mod:`pyestat._engine.loader` — YAML loader for the schema.
* :mod:`pyestat._engine.fingerprint` — structural fingerprint of a response.
* :mod:`pyestat._engine.matchers` — Matcher pipeline (statsCode + fingerprint).
* :mod:`pyestat._engine.transformers` — Transformer pipeline (time / value).
* :mod:`pyestat._engine.manager` — RuleManager (resolution chain).
* :mod:`pyestat._engine.apply` — glue that runs the resolved rule over rows.
* :mod:`pyestat._engine.builtin` — loader for library-bundled rules.

Public symbols (``EstatClient``, ``Rule``, ``load_builtin_rules`` …)
re-export from :mod:`pyestat`. Direct ``pyestat._engine.X`` imports are
internal.
"""
