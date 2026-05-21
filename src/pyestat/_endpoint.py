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
from typing import Any, Literal

from pyestat._http import EstatHttpClient, ProgressEvent
from pyestat.errors import EstatApiError, TooManyRowsError


# --- response models -------------------------------------------------------


@dataclass(frozen=True)
class ClassObj:
    """One axis from ``CLASS_INF.CLASS_OBJ``.

    ``classes`` is the flattened list of ``CLASS`` entries — ``@code``,
    ``@name``, ``@level``, ``@parentCode``, ``@unit`` etc. with the ``@``
    prefix stripped. Names are kept raw; normalization for fingerprint
    matching happens in Layer 3.
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
    """Aggregated result of :meth:`EstatClient.get_stats_data`."""

    stats_data_id: str
    total_number: int | None
    table_inf: dict[str, Any]
    class_objs: tuple[ClassObj, ...]
    values: tuple[dict[str, Any], ...]


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
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        http: EstatHttpClient | None = None,
        builtin_rules: "Sequence[Any] | None" = None,
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
        from pyestat._engine.manager import RuleManager

        resolved_builtins = list(builtin_rules) if builtin_rules is not None else load_builtin_rules()
        self._rule_manager = RuleManager(builtin=resolved_builtins)

    # ----- getStatsData -----

    def get_stats_data(
        self,
        stats_data_id: str,
        *,
        rule: "Rule | Literal['auto', 'heuristic'] | None" = "auto",
        max_rows: int | None = None,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> StatsDataResponse:
        """Fetch one table, walking ``NEXT_KEY`` until all rows are pulled.

        ``rule`` selects the transformation mode (Decision B / E):

        * ``"auto"`` (default) — try the rule resolution chain
          (user > project > builtin); if any layer matches, apply
          that rule; otherwise fall back to ``"heuristic"``.
        * ``"heuristic"`` — label substitution only (each axis gets a
          ``{axis_id}_label`` field); no standard-code mapping and no
          value typing. Useful when you want predictable output
          regardless of which built-in rules ship with the library.
        * ``None`` — raw mode. Returns Layer 2's untransformed flattened
          rows verbatim.
        * :class:`Rule` — apply this rule directly, bypassing the
          resolution chain.

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
        raw = StatsDataResponse(
            stats_data_id=stats_data_id,
            total_number=first.total_number,
            table_inf=first.table_inf,
            class_objs=first.class_objs,
            values=values,
        )
        # Imported lazily so the (L3 → L2) dependency direction stays
        # one-way: ``_apply.py`` consumes ``ClassObj`` from this module.
        from pyestat._engine.apply import apply_rule

        resolved = self._resolve_rule(raw, rule)
        transformed = apply_rule(values, first.class_objs, stats_data_id, resolved)
        return StatsDataResponse(
            stats_data_id=stats_data_id,
            total_number=first.total_number,
            table_inf=first.table_inf,
            class_objs=first.class_objs,
            values=transformed,
        )

    def _resolve_rule(
        self,
        raw: "StatsDataResponse",
        rule: "Rule | Literal['auto', 'heuristic'] | None",
    ) -> "Rule | Literal['heuristic'] | None":
        """Collapse the ``"auto"`` fallback into a concrete decision.

        ``"auto"`` consults the rule manager first and falls back to
        ``"heuristic"`` when nothing matched; everything else passes
        through untouched.
        """
        if rule == "auto":
            match = self._rule_manager.select(raw)
            return match if match is not None else "heuristic"
        return rule  # type: ignore[return-value]

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

        Used by Layer 3's fingerprint matcher to validate a rule's
        applicability before committing to a potentially huge fetch.
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
