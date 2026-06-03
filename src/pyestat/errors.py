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


class RuleExpansionError(EstatError):
    """A v2 short-form rule column could not be expanded to long form.

    Raised at rule-load / expansion time — an *authoring* error (the
    column omits its source yet its name is not a role to infer one
    from). It is a typed :class:`EstatError` so the auto-path wiring
    (#28) can tell it apart from a genuine I/O failure; in practice the
    auto path never surfaces it to a caller, because built-in rules are
    validated in CI and explicitly-passed rules are the caller's own.
    """

    def __init__(self, *, column: str, reason: str) -> None:
        super().__init__(f"cannot expand output column {column!r}: {reason}")
        self.column = column
        self.reason = reason


class RoleResolutionError(EstatError):
    """A v2 rule references a role the classification cannot pin to one axis.

    Either no axis carries the role (e.g. an ``area`` column on an
    area-less table) or several do (the pivot case, #10, which needs a
    ``where`` predicate this MVP does not have). Typed so the auto-path
    wiring (#28) catches it and falls back to Layer D — preserving the
    caller's data — rather than letting it surface.
    """

    def __init__(self, *, role: object, reason: str) -> None:
        super().__init__(f"cannot resolve role {role!r}: {reason}")
        self.role = role
        self.reason = reason


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
