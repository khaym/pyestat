"""Layer 2: Endpoint surface.

Maps Python kwargs to e-Stat query parameters, parses the JSON response
into typed dataclasses, raises :class:`EstatApiError` on
``RESULT.STATUS != 0``, and walks ``NEXT_KEY`` pagination. Transport
mechanics (retry, timeout, ``appId`` injection) live in Layer 1.

Out of scope here: rule matching, label substitution, standard-code
normalization — those are Layer 3.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyestat._http import EstatHttpClient, ProgressEvent
from pyestat.errors import EstatApiError, TooManyRowsError

if TYPE_CHECKING:
    from pyestat._engine.rule import RuleV2


# --- response models -------------------------------------------------------


@dataclass(frozen=True)
class ClassObj:
    """One axis from ``CLASS_INF.CLASS_OBJ``.

    ``classes`` is the flattened list of ``CLASS`` entries — ``@code``,
    ``@name``, ``@level``, ``@parentCode``, ``@unit`` etc. with the ``@``
    prefix stripped. Names are kept raw; any normalization (e.g. the
    axis classifier's NFKC folding) happens in Layer 3.
    """

    id: str
    name: str
    classes: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Page:
    """One physical page of a ``getStatsData`` response.

    Each page carries the full ``table_inf`` / ``class_objs`` so a caller
    can consume pages independently without keeping the first page
    around. ``next_key`` is ``None`` on the final page.
    """

    page_number: int
    values: tuple[dict[str, Any], ...]
    next_key: int | None
    total_number: int | None
    table_inf: dict[str, Any]
    class_objs: tuple[ClassObj, ...]


@dataclass(frozen=True)
class StatsDataResponse:
    """Aggregated result of :meth:`EstatClient.get_stats_data`.

    ``values`` is the canonical *nested* form (#35): each field is a
    self-describing object — a ``{code, label}`` dimension, a time cell
    (``{code, label, normalized, granularity}``), or a ``{value, unit}``
    measure — so an agent reads ``row["cat01"]["label"]`` without a suffix
    convention. :meth:`to_flat` projects to one column per field for callers
    who prefer the flat shape. A raw (``rule=None``) response keeps Layer 2's
    flat rows; :meth:`to_flat` leaves them unchanged.
    """

    stats_data_id: str
    total_number: int | None
    table_inf: dict[str, Any]
    class_objs: tuple[ClassObj, ...]
    values: tuple[dict[str, Any], ...]

    def to_flat(self) -> tuple[dict[str, Any], ...]:
        """Project the nested ``values`` to the flat suffix convention.

        A ``{code, label}`` dimension flattens to ``K`` / ``K_label``; a time
        cell to ``K`` (normalized) / ``K_code`` / ``K_label`` /
        ``K_granularity``; a ``{value, unit}`` measure to ``K`` plus its unit
        (the lone observation column's unit takes the bare ``unit`` key, a
        pivot measure's a per-column ``K_unit``). Lossless and idempotent — an
        already-flat (``rule=None``) row passes through untouched. For a
        DataFrame: ``pandas.DataFrame(resp.to_flat())``.
        """
        # Lazy import keeps the L2 → L3 dependency out of module-load time
        # (the rule subsystem imports this module, not the other way around).
        from pyestat._engine.canonical import to_flat_rows

        return to_flat_rows(self.values)


@dataclass(frozen=True)
class MetaInfoResponse:
    """Result of :meth:`EstatClient.get_meta_info`."""

    stats_data_id: str
    table_inf: dict[str, Any]
    class_objs: tuple[ClassObj, ...]


@dataclass(frozen=True)
class StatsListResponse:
    """Result of :meth:`EstatClient.list_stats`.

    ``tables`` is intentionally typed as raw dicts: ``TABLE_INF`` schema
    drifts across statistics families and modeling it would slow down
    keeping pyestat current with the search API.
    """

    total_number: int
    tables: tuple[dict[str, Any], ...]


# --- helpers ---------------------------------------------------------------


def _ensure_list(x: Any) -> list[Any]:
    """Normalize e-Stat's "single value collapses to a bare dict" quirk.

    The API inlines a one-element array as the underlying dict whenever
    it can; downstream iteration over ``dict`` keys silently produces
    the wrong result, so the fix-up is centralized here.
    """
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def _flatten(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Strip ``@`` prefixes and rename ``$`` to ``value``.

    Layer 2's only structural rewrite; every other transformation
    (label substitution, standard-code mapping, value casting) is
    Layer 3's responsibility.
    """
    result: dict[str, Any] = {}
    for key, val in entry.items():
        if key.startswith("@"):
            result[key[1:]] = val
        elif key == "$":
            result["value"] = val
        else:
            result[key] = val
    return result


def _parse_class_objs(class_inf: Mapping[str, Any] | None) -> tuple[ClassObj, ...]:
    if not class_inf:
        return ()
    result: list[ClassObj] = []
    for obj in _ensure_list(class_inf.get("CLASS_OBJ")):
        result.append(
            ClassObj(
                id=obj["@id"],
                name=obj["@name"],
                classes=tuple(_flatten(c) for c in _ensure_list(obj.get("CLASS"))),
            )
        )
    return tuple(result)


def _check_status(result: Mapping[str, Any]) -> None:
    status = result.get("STATUS", 0)
    if status != 0:
        raise EstatApiError(status=status, message=result.get("ERROR_MSG", ""))


# --- client ----------------------------------------------------------------


class EstatClient:
    """High-level e-Stat API client (sync).

    Constructed with an injected :class:`EstatHttpClient` rather than
    raw config so tests can supply a mock transport without monkey-
    patching, and so future async / cached variants can swap the
    transport without touching this surface.

    ``user_rules`` injects caller-defined v2 rules into the top precedence
    layer of the resolution chain (``user > project > builtin``); the
    ``"auto"`` path resolves them by role pattern. A user rule matching a
    table's role pattern shadows a built-in for the same pattern; an
    unrelated user rule does not block built-ins from firing on other
    tables.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        http: EstatHttpClient | None = None,
        builtin_rules: "Sequence[RuleV2] | None" = None,
        user_rules: "Sequence[RuleV2] | None" = None,
    ) -> None:
        if http is None:
            if app_id is None:
                raise ValueError("Either app_id or http is required")
            http = EstatHttpClient(app_id=app_id)
        self._http = http
        # Imported lazily to keep the import graph one-way: the rule
        # subsystem may depend on the endpoint module, but not the
        # other way around at module-import time.
        from pyestat._engine.builtin import load_builtin_rules

        # All three layers hold v2 rules; the auto path resolves them by
        # role pattern. Project-local rules (#15) will populate the middle
        # layer; until then it is empty.
        self._user_rules: list[RuleV2] = (
            list(user_rules) if user_rules is not None else []
        )
        self._project_rules: list[RuleV2] = []
        self._builtin_rules: list[RuleV2] = (
            list(builtin_rules) if builtin_rules is not None else load_builtin_rules()
        )

    # ----- getStatsData -----

    def get_stats_data(
        self,
        stats_data_id: str,
        *,
        rule: "RuleV2 | Literal['auto', 'heuristic'] | None" = "auto",
        aggregates: Literal["include", "exclude", "only"] = "include",
        max_rows: int | None = None,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> StatsDataResponse:
        """Fetch one table, walking ``NEXT_KEY`` until all rows are pulled.

        Every transformed mode returns the canonical *nested* row shape (#35):
        each axis is a ``{code, label}`` cell (``time`` adds ``normalized`` /
        ``granularity``) and the observation is a ``{value, unit}`` measure.
        Call :meth:`StatsDataResponse.to_flat` for the one-column-per-field
        flat shape (pandas). ``rule`` selects the transformation mode:

        * ``"auto"`` (default) — classify the table's axes, then resolve a
          rule through Layers C > B > A > D: a matching v2 rule
          (user/project, then built-in), else a generic rule built from the
          classified roles (Layer A), else the Layer D fallback when the
          table cannot be structured (a low-confidence axis, or a shape
          needing a pivot this MVP lacks). A rule *you* supplied via
          ``user_rules`` that then fails to apply surfaces as a typed
          :class:`EstatError` so you can fix it; a built-in or generic rule
          that fails degrades to Layer D instead (``docs/DESIGN.md``
          Decision B).
        * ``"heuristic"`` — Layer D fallback. The axis classifier detects
          the ``time`` axis and normalizes it best-effort; every axis becomes
          a ``{code, label}`` cell. Raw codes are preserved (in each cell's
          ``code``), the cell value is never coerced, and an unrecognized time
          code keeps ``normalized == code`` — data is preserved, axes are not
          normalized to standard codes (that is out of scope here). Useful
          when you want predictable, lossless output regardless of which
          built-in rules ship.
        * ``None`` — raw mode. Returns Layer 2's untransformed flattened
          rows verbatim (flat scalars, not nested cells).
        * :class:`RuleV2` — apply this rule directly against the table's
          classification, bypassing the resolution chain.

        ``aggregates`` selects which rows of a hierarchical table you receive,
        independent of ``rule``. e-Stat marks a code hierarchy with
        ``@parentCode`` (総数 → 大分類 → 品目, 全国 → 都道府県); summing a measure
        across a total and its children double-counts. The filter runs on the
        raw rows before any rule, so every mode honors it:

        * ``"include"`` (default) — every row; today's behavior, unchanged.
        * ``"exclude"`` — drop the aggregates, keeping only the leaves (the
          detail grain), so the result is safe to sum. With several
          hierarchical dimensions a row is kept only when it is a leaf on every
          one.
        * ``"only"`` — keep the aggregates (subtotals / totals), the exact
          complement of ``"exclude"``.

        Detection is per-response and ``category`` / ``area`` only: a code is
        an aggregate when a child of it is present in the fetched rows, so a
        table holding just a total is not filtered. A hierarchy e-Stat ships
        without ``@parentCode`` is invisible to this filter.

        When ``max_rows`` is set, a cheap ``cntGetFlg=Y`` probe runs first
        and the call raises :class:`TooManyRowsError` before any data page
        is downloaded if the table exceeds the cap.
        """
        if max_rows is not None:
            payload = self._http.request(
                "/getStatsData",
                params={"statsDataId": stats_data_id, "cntGetFlg": "Y"},
            )
            root = payload["GET_STATS_DATA"]
            _check_status(root["RESULT"])
            total = root["STATISTICAL_DATA"]["RESULT_INF"]["TOTAL_NUMBER"]
            if total > max_rows:
                raise TooManyRowsError(
                    stats_data_id=stats_data_id, total=total, limit=max_rows
                )

        pages = list(self.iter_stats_data_pages(stats_data_id, progress=progress))
        first = pages[0]
        values = tuple(v for p in pages for v in p.values)
        # Imported lazily so the (L3 → L2) dependency direction stays
        # one-way: the rule subsystem consumes ``ClassObj`` from this module.
        from pyestat._engine.apply import apply_auto, apply_rule

        # Classify once, with the *unfiltered* rows, so #27's data-driven
        # meta-axis signal sees the whole table; the result feeds the aggregate
        # selection (#36), resolution, v2 application, and the Layer D
        # fallback. Computed when "auto" needs it or an aggregate selection
        # does — raw mode with the default still never classifies.
        classification = None
        if rule == "auto" or aggregates != "include":
            from pyestat._engine.classifier import classify

            classification = classify(first.class_objs, first.table_inf, rows=values)

        if aggregates != "include":
            # Filter detail / aggregate rows before any rule runs, so every
            # mode honors the selection and a downstream pivot folds only the
            # chosen grain (#36).
            from pyestat._engine.aggregate import select_rows

            values = select_rows(values, classification, first.class_objs, aggregates)

        if rule == "auto":
            from pyestat._engine.resolver import resolve_v2

            resolved = resolve_v2(
                classification,
                user=self._user_rules,
                project=self._project_rules,
                builtin=self._builtin_rules,
                class_objs=first.class_objs,
                stats_data_id=stats_data_id,
            )
            transformed = apply_auto(values, first.class_objs, classification, resolved)
        else:
            # raw (``None``) and the explicit modes classify lazily inside
            # ``apply_rule`` only when needed; pass any classification already
            # computed for the aggregate selection so it is not recomputed on
            # the post-filter rows.
            transformed = apply_rule(
                values, first.class_objs, stats_data_id, rule, classification
            )
        return StatsDataResponse(
            stats_data_id=stats_data_id,
            total_number=first.total_number,
            table_inf=first.table_inf,
            class_objs=first.class_objs,
            values=transformed,
        )

    def iter_stats_data_pages(
        self,
        stats_data_id: str,
        *,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> Iterator[Page]:
        """Yield each ``NEXT_KEY`` page one at a time.

        Lower-level than :meth:`get_stats_data`: callers can stream a
        3.8M-row table without materializing the whole list. ``progress``
        is fired *after* each page has been parsed, so a tqdm bridge
        sees the count reflect what was actually received.
        """
        next_key: int | None = None
        page_number = 0
        rows_fetched = 0
        page_size: int | None = None
        while True:
            page_number += 1
            params: dict[str, Any] = {"statsDataId": stats_data_id}
            if next_key is not None:
                params["startPosition"] = next_key
            payload = self._http.request("/getStatsData", params=params)
            page = self._parse_page(payload, page_number)
            rows_fetched += len(page.values)
            if page_size is None and page.values:
                page_size = len(page.values)
            if progress is not None:
                total_pages = (
                    math.ceil(page.total_number / page_size)
                    if page.total_number and page_size
                    else None
                )
                progress(
                    ProgressEvent(
                        page=page_number,
                        total_pages=total_pages,
                        rows_fetched=rows_fetched,
                        rows_total=page.total_number,
                    )
                )
            yield page
            if page.next_key is None:
                break
            next_key = page.next_key

    @staticmethod
    def _parse_page(payload: Mapping[str, Any], page_number: int) -> Page:
        root = payload["GET_STATS_DATA"]
        _check_status(root["RESULT"])
        sd = root["STATISTICAL_DATA"]
        result_inf = sd.get("RESULT_INF", {})
        next_key_raw = result_inf.get("NEXT_KEY")
        next_key = int(next_key_raw) if next_key_raw is not None else None
        return Page(
            page_number=page_number,
            values=tuple(_flatten(v) for v in _ensure_list(sd.get("DATA_INF", {}).get("VALUE"))),
            next_key=next_key,
            total_number=result_inf.get("TOTAL_NUMBER"),
            table_inf=dict(sd.get("TABLE_INF", {})),
            class_objs=_parse_class_objs(sd.get("CLASS_INF")),
        )

    # ----- getMetaInfo -----

    def get_meta_info(self, stats_data_id: str) -> MetaInfoResponse:
        """Fetch axis metadata without downloading data.

        Lets a caller inspect a table's axes before committing to a
        potentially huge fetch.
        """
        payload = self._http.request(
            "/getMetaInfo", params={"statsDataId": stats_data_id}
        )
        root = payload["GET_META_INFO"]
        _check_status(root["RESULT"])
        metadata = root.get("METADATA_INF", {})
        return MetaInfoResponse(
            stats_data_id=stats_data_id,
            table_inf=dict(metadata.get("TABLE_INF", {})),
            class_objs=_parse_class_objs(metadata.get("CLASS_INF")),
        )

    # ----- getStatsList -----

    def list_stats(self, **params: Any) -> StatsListResponse:
        """Search the e-Stat catalog.

        Parameters are forwarded raw because the search API has many
        rarely-used knobs (``searchWord``, ``statsCode``, ``surveyYears``,
        ``openYears``, ``statsField``…); a Python-side enumeration
        would lag behind the published API without adding safety.
        """
        payload = self._http.request("/getStatsList", params=params)
        root = payload["GET_STATS_LIST"]
        _check_status(root["RESULT"])
        dl = root.get("DATALIST_INF", {})
        result_inf = dl.get("RESULT_INF", {})
        tables = tuple(_ensure_list(dl.get("TABLE_INF")))
        return StatsListResponse(
            total_number=result_inf.get("TOTAL_NUMBER", len(tables)),
            tables=tables,
        )
