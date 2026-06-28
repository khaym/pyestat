"""Python client for the e-Stat API.

The names re-exported here are pyestat's public surface. For the 0.x series
stability splits two ways:

* **settled** (stability promised) — the consumption path: :class:`EstatClient`
  and its getters (``get_stats_data``, ``get_meta_info``, ``list_stats``,
  ``iter_stats_data_pages``); the response objects :class:`StatsDataResponse`
  (and its ``to_flat``), :class:`MetaInfoResponse`, :class:`StatsListResponse`,
  :class:`Page`, :class:`ClassObj`; :class:`EstatHttpClient`,
  :class:`ProgressEvent`; and the error hierarchy :class:`EstatError`,
  :class:`EstatApiError`, :class:`HttpRetryExhaustedError`,
  :class:`TooManyRowsError`, :class:`AmbiguousRuleError`.
* **evolving** (may change during 0.x) — the rule-authoring path:
  :class:`RuleV2`, :func:`load_builtin_rules`, and the
  :class:`RuleAuthoringError` category. The rule schema is not frozen yet.

The authoring *leaf* errors (``RoleResolutionError``, ``RuleExpansionError``,
``UnknownTransformError``, ``TimeFormatError``) and the rule-file
``RuleLoadError`` are intentionally not re-exported. Reach them through
``pyestat._errors`` if you must, accepting that an underscore path carries no
stability promise; a coarse ``except EstatError`` catches them all regardless.
"""
from pyestat._endpoint import (
    ClassObj,
    EstatClient,
    MetaInfoResponse,
    Page,
    StatsDataResponse,
    StatsListResponse,
)
from pyestat._engine.builtin import load_builtin_rules
from pyestat._engine.rule import RuleV2
from pyestat._errors import (
    AmbiguousRuleError,
    EstatApiError,
    EstatError,
    HttpRetryExhaustedError,
    RuleAuthoringError,
    TooManyRowsError,
)
from pyestat._http import EstatHttpClient, ProgressEvent


__all__ = [
    "AmbiguousRuleError",
    "ClassObj",
    "EstatApiError",
    "EstatClient",
    "EstatError",
    "EstatHttpClient",
    "HttpRetryExhaustedError",
    "MetaInfoResponse",
    "Page",
    "ProgressEvent",
    "RuleAuthoringError",
    "RuleV2",
    "StatsDataResponse",
    "StatsListResponse",
    "TooManyRowsError",
    "load_builtin_rules",
]
