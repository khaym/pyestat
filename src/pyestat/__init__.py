"""Python client for the e-Stat API.

Public surface spans all four layers:

* Layer 1 — :class:`EstatHttpClient`, :class:`ProgressEvent`.
* Layer 2 — :class:`EstatClient`, :class:`StatsDataResponse`,
  :class:`MetaInfoResponse`, :class:`StatsListResponse`,
  :class:`Page`, :class:`ClassObj`.
* Layer 3 — :class:`RuleV2`, :func:`load_builtin_rules`.
* Errors — :class:`EstatError` and its leaves.
"""
from pyestat._engine.builtin import load_builtin_rules
from pyestat._endpoint import (
    ClassObj,
    EstatClient,
    MetaInfoResponse,
    Page,
    StatsDataResponse,
    StatsListResponse,
)
from pyestat._http import EstatHttpClient, ProgressEvent
from pyestat._engine.rule import RuleV2
from pyestat.errors import (
    AmbiguousRuleError,
    EstatApiError,
    EstatError,
    HttpRetryExhaustedError,
    RoleResolutionError,
    RuleAuthoringError,
    RuleExpansionError,
    TooManyRowsError,
    UnknownTransformError,
)


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
    "RoleResolutionError",
    "RuleAuthoringError",
    "RuleExpansionError",
    "RuleV2",
    "StatsDataResponse",
    "StatsListResponse",
    "TooManyRowsError",
    "UnknownTransformError",
    "load_builtin_rules",
]
