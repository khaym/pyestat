"""Python client for the e-Stat API.

Public surface: HTTP client (Layer 1) + endpoint client (Layer 2).
Rule-engine symbols (Layer 3) will be added once that layer lands.
"""
from pyestat._endpoint import (
    ClassObj,
    EstatClient,
    MetaInfoResponse,
    Page,
    StatsDataResponse,
    StatsListResponse,
)
from pyestat._http import EstatHttpClient, ProgressEvent
from pyestat.errors import (
    EstatApiError,
    EstatError,
    HttpRetryExhaustedError,
    TooManyRowsError,
)


__all__ = [
    "ClassObj",
    "EstatApiError",
    "EstatClient",
    "EstatError",
    "EstatHttpClient",
    "HttpRetryExhaustedError",
    "MetaInfoResponse",
    "Page",
    "ProgressEvent",
    "StatsDataResponse",
    "StatsListResponse",
    "TooManyRowsError",
]
