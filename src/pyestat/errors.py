"""Exception hierarchy for pyestat.

A small surface, deliberately. Three concrete leaves cover the
distinctions callers actually need to act on:

* :class:`HttpRetryExhaustedError` — transport is broken; back off.
* :class:`EstatApiError` — the API rejected the *query* (bad statsDataId,
  unknown parameter); the request itself succeeded.
* :class:`TooManyRowsError` — the table exists but is bigger than the
  caller is willing to pull; raised before any data is downloaded.

All inherit from :class:`EstatError` so a coarse ``except EstatError`` is
enough when the caller does not need to discriminate.
"""
from __future__ import annotations


class EstatError(Exception):
    """Base class for all pyestat-raised errors."""


class HttpRetryExhaustedError(EstatError):
    """All retry attempts failed for a single HTTP request.

    Distinct from a logical :class:`EstatApiError` so callers can tell
    a transient outage apart from a malformed query.
    """

    def __init__(
        self,
        *,
        attempts: int,
        last_status: int | None,
        last_exc: Exception | None,
    ) -> None:
        suffix = (
            f"HTTP {last_status}" if last_status is not None
            else f"{type(last_exc).__name__}: {last_exc}"
        )
        super().__init__(
            f"e-Stat HTTP request failed after {attempts} attempts ({suffix})"
        )
        self.attempts = attempts
        self.last_status = last_status
        self.last_exc = last_exc


class EstatApiError(EstatError):
    """e-Stat returned ``RESULT.STATUS != 0``.

    e-Stat reports query-level problems with HTTP 200 and a non-zero
    ``RESULT.STATUS``; transport success does not imply a successful
    query, so Layer 2 promotes that signal into an exception.
    """

    def __init__(self, *, status: int, message: str) -> None:
        super().__init__(f"e-Stat API error (status={status}): {message}")
        self.status = status
        self.message = message


class AmbiguousRuleError(EstatError):
    """Two rules at the same precedence layer matched the same response.

    Surfacing the conflict (rather than silently picking one) prevents
    a typo in a bundled rule file from quietly masking a user's
    project-local rule.
    """

    def __init__(self, *, stats_data_id: str, matched_rules: list) -> None:
        super().__init__(
            f"Multiple rules matched {stats_data_id}: "
            f"{[r.match.statsCode for r in matched_rules]}"
        )
        self.stats_data_id = stats_data_id
        self.matched_rules = matched_rules


class TooManyRowsError(EstatError):
    """The requested table exceeds the caller-supplied ``max_rows`` cap.

    Raised by :meth:`pyestat.EstatClient.get_stats_data` after the
    pre-flight ``cntGetFlg=Y`` count check, before any data page is
    downloaded — so the caller can resize ``max_rows`` (or paginate
    via ``iter_stats_data_pages``) without having paid for the table.
    """

    def __init__(self, *, stats_data_id: str, total: int, limit: int) -> None:
        super().__init__(
            f"Table {stats_data_id} has {total} rows, exceeds max_rows={limit}"
        )
        self.stats_data_id = stats_data_id
        self.total = total
        self.limit = limit
