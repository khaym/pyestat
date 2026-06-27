"""Exception hierarchy for pyestat.

A small surface, deliberately. Three concrete leaves cover the
transport- and query-level distinctions callers act on:

* :class:`HttpRetryExhaustedError` — transport is broken; back off.
* :class:`EstatApiError` — the API rejected the *query* (bad statsDataId,
  unknown parameter); the request itself succeeded.
* :class:`TooManyRowsError` — the table exists but is bigger than the
  caller is willing to pull; raised before any data is downloaded.

A second group, :class:`RuleAuthoringError` and its leaves
(:class:`RoleResolutionError`, :class:`RuleExpansionError`,
:class:`UnknownTransformError`, :class:`TimeFormatError`), reports a rule
that cannot be applied as authored. On the ``rule="auto"`` path, whether such a failure surfaces or
quietly degrades to the lossless Layer D fallback turns on *who authored
the failing rule*: a rule the caller passed or wrote (an explicit
``rule=``, or a user / project rule) surfaces so they can fix it; a
library-provided rule (a built-in, or the generic rule derived from axis
roles) degrades, since the caller cannot fix it and preserved raw data
beats a crash. See ``docs/DESIGN.md`` Decision B for the full policy and
its decision table.

A third rule-level leaf, :class:`RuleLoadError`, reports a rule *file*
that could not be read, parsed, or validated into a rule at all — distinct
from a :class:`RuleAuthoringError`, which is a rule that loaded but cannot
be applied to a given table.

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


def _describe_match(rule: object) -> object:
    """A short, human label for a conflicting rule — its ``role_pattern`` —
    so the ambiguity error names the colliding rules legibly. ``getattr``
    keeps it total: anything that is not a well-formed rule falls back to
    its ``repr`` rather than raising inside the error path."""
    match = getattr(rule, "match", None)
    role_pattern = getattr(match, "role_pattern", None)
    if role_pattern is not None:
        return [getattr(role, "value", role) for role in role_pattern]
    return repr(rule)


class AmbiguousRuleError(EstatError):
    """Two rules at the same precedence layer matched the same response.

    Surfacing the conflict (rather than silently picking one) prevents
    a typo in a bundled rule file from quietly masking a user's
    project-local rule.
    """

    def __init__(self, *, stats_data_id: str, matched_rules: list) -> None:
        super().__init__(
            f"Multiple rules matched {stats_data_id}: "
            f"{[_describe_match(r) for r in matched_rules]}"
        )
        self.stats_data_id = stats_data_id
        self.matched_rules = matched_rules


class RuleLoadError(EstatError):
    """A rule file could not be read, parsed, or validated into a rule.

    Raised by the YAML loader when a file cannot become a ``RuleV2``: it is
    unreadable, is not valid YAML, has a non-mapping top level, names an
    unsupported ``schema_version``, or fails schema validation. Distinct
    from :class:`RuleAuthoringError`, which is a rule that loaded but cannot
    be *applied* to a particular table. Wrapping the underlying ``yaml`` /
    ``pydantic`` / ``OSError`` in a typed error keeps the coarse
    ``except EstatError`` contract whole for a caller who dropped a bad file
    in their project rules directory (#15).
    """

    def __init__(self, *, path: object, reason: str) -> None:
        super().__init__(f"cannot load rule file {path}: {reason}")
        self.path = path
        self.reason = reason


class RuleAuthoringError(EstatError):
    """A rule cannot be applied as authored.

    The shared base of the ways a single rule fails at apply time —
    :class:`RoleResolutionError`, :class:`RuleExpansionError`,
    :class:`UnknownTransformError`, and :class:`TimeFormatError`. Grouping
    them lets the ``rule="auto"``
    path catch the whole category in one place and route it by provenance
    (a caller-authored rule surfaces, a library-provided one degrades to
    Layer D — see ``docs/DESIGN.md`` Decision B), and lets a caller catch
    the category with a single ``except``. Whether such an error reaches
    the caller therefore depends on who authored the rule, not on the leaf
    type.
    """


class RuleExpansionError(RuleAuthoringError):
    """A v2 short-form rule column could not be expanded to long form.

    Raised at rule-load / expansion time — an *authoring* error (the
    column omits its source yet its name is not a role to infer one from).
    As a :class:`RuleAuthoringError`, whether the auto path surfaces it or
    degrades to Layer D follows the provenance policy (see that base and
    ``docs/DESIGN.md`` Decision B).
    """

    def __init__(self, *, column: str, reason: str) -> None:
        super().__init__(f"cannot expand output column {column!r}: {reason}")
        self.column = column
        self.reason = reason


class RoleResolutionError(RuleAuthoringError):
    """A v2 rule references a role the classification cannot pin to one axis.

    Raised when no axis carries the role (e.g. an ``area`` column on an
    area-less table), when a repeated non-meta role stays ambiguous (no way
    to address one of several same-role axes yet), or when a ``meta-axis``
    pivot (#10) cannot bind — a missing/duplicate meta-axis, a ``where``-less
    meta column, or absent class metadata. As a :class:`RuleAuthoringError`,
    the auto path surfaces or degrades it by provenance (``docs/DESIGN.md``
    Decision B).
    """

    def __init__(self, *, role: object, reason: str) -> None:
        super().__init__(f"cannot resolve role {role!r}: {reason}")
        self.role = role
        self.reason = reason


class UnknownTransformError(RuleAuthoringError):
    """A v2 rule column names a transform the registry does not know.

    A rule-authoring error — a typo, or a transform a newer pyestat
    registers that this version lacks. As a :class:`RuleAuthoringError` it
    is routed by provenance on the auto path (``docs/DESIGN.md`` Decision
    B), and being typed it never reaches a caller as a bare ``KeyError``.
    Carries the offending column and the known transform names so the
    message is actionable.
    """

    def __init__(self, *, column: str, transform: str, known: list[str]) -> None:
        super().__init__(
            f"unknown transform {transform!r} for output column {column!r} "
            f"(known: {known})"
        )
        self.column = column
        self.transform = transform
        self.known = known


class TimeFormatError(RuleAuthoringError):
    """A time column cannot produce a normalized time point as authored.

    Two cases, both authoring decisions: the column names a transform that is
    not a time format (a time column must declare ``best_effort_time`` or a
    specific parser like ``yearly``), or a *strict* declared format rejects an
    actual code because the table's time shape does not match the format the
    rule chose. The role-default ``best_effort_time`` is total and never lands
    here; only an explicitly chosen strict format can. As a
    :class:`RuleAuthoringError` the auto path routes it by provenance — a
    caller-authored rule surfaces so they can pick the right format, a
    built-in degrades to Layer D (``docs/DESIGN.md`` Decision B) — so a
    declared format is honored rather than silently replaced by a guess.
    """

    def __init__(
        self, *, column: str, transform: object, reason: str, code: object = None
    ) -> None:
        detail = f" (code {code!r})" if code is not None else ""
        super().__init__(
            f"time column {column!r} cannot apply format {transform!r}{detail}: {reason}"
        )
        self.column = column
        self.transform = transform
        self.reason = reason
        self.code = code


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
