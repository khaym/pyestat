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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pyestat._http import EstatHttpClient, ProgressEvent
from pyestat._errors import EstatApiError, TooManyRowsError

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

    ``values`` is the canonical *nested* form: each field is a
    self-describing object — a ``{code, label}`` dimension, a time cell
    (``{code, label, normalized, granularity}``), or a ``{value, unit}``
    measure — so an agent reads ``row["cat01"]["label"]`` without a suffix
    convention. :meth:`to_flat` projects to one column per field for callers
    who prefer the flat shape. A raw (``rule=None``) response keeps Layer 2's
    flat rows; :meth:`to_flat` leaves them unchanged.

    Two properties of this shape are part of the contract:

    * **Values are the raw e-Stat strings, never coerced.** A measure's
      ``value`` and every ``code`` stay exactly as e-Stat sent them — numbers
      arrive as strings (``"1097352"``) and suppression markers (``"-"`` /
      ``"***"`` / ``"X"``) pass through verbatim. Casting is the caller's: a
      guessed numeric type would corrupt those markers, so a pandas user
      applies ``pd.to_numeric(..., errors="coerce")`` themselves. Only a time
      cell's ``normalized`` / ``granularity`` are derived (best-effort); the
      observation ``value`` is never touched.
    * **Row keys depend on the mode.** Under ``"auto"`` / ``"heuristic"`` the
      keys are e-Stat's own axis ids (``cat01``, ``area``, ``time`` …) plus
      ``value`` for the observation — opaque and table-specific, so what an
      axis *means* lives in :attr:`class_objs`, not the key. Stable, semantic
      column names (``commodity``, ``month`` …) come only from a rule; a pivot
      names its folded columns by the meta-axis member name.
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

    The ``"auto"`` path resolves rules by role pattern through three layers,
    ``user > project > builtin``; a rule in a higher layer shadows a lower
    one matching the same pattern, while an unrelated rule leaves the lower
    layers free to fire on other tables.

    * ``user_rules`` — caller-defined v2 rules injected into the top layer.
    * ``project_rules_dir`` — a directory of ``*.yaml`` / ``*.yml`` rules
      auto-discovered into the middle layer, the escape hatch for
      tables no built-in covers: drop a rule file in the directory and it
      applies with no code change. Defaults to ``"pyestat_rules"`` (i.e.
      ``./pyestat_rules`` relative to the working directory); pass another
      path to relocate it, or ``None`` / ``""`` to opt out. The
      pyestat-specific name keeps a plain client from silently adopting an
      unrelated directory's rules. An absent directory means "no project
      rules", not an error, so the common no-rules case never raises;
      discovery is working-directory dependent, and a *malformed* file in the
      directory raises :class:`RuleLoadError` at construction (the caller
      authored it, so it surfaces — ARCHITECTURE.md).
    * ``builtin_rules`` — the library-bundled rules (the bottom layer),
      loaded from the package by default.
    """

    def __init__(
        self,
        *,
        app_id: str | None = None,
        http: EstatHttpClient | None = None,
        builtin_rules: "Sequence[RuleV2] | None" = None,
        user_rules: "Sequence[RuleV2] | None" = None,
        project_rules_dir: "str | Path | None" = "pyestat_rules",
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
        from pyestat._engine.loader import YamlRuleLoader

        # All three layers hold v2 rules; the auto path resolves them by role
        # pattern (user > project > builtin). The project layer is populated by
        # scanning ``project_rules_dir`` so a caller drops a YAML in the
        # directory and it applies without editing code. Any falsy value
        # (``None`` / ``""``) opts out — the latter matters because ``Path("")``
        # would otherwise collapse to the cwd and scan it. ``load_dir`` returns
        # [] for an absent directory, so a missing default ``./pyestat_rules``
        # is a no-op; a malformed file present in the directory raises
        # RuleLoadError (the caller authored it, so it surfaces — ARCHITECTURE.md).
        self._user_rules: list[RuleV2] = (
            list(user_rules) if user_rules is not None else []
        )
        self._project_rules: list[RuleV2] = (
            YamlRuleLoader().load_dir(Path(project_rules_dir))
            if project_rules_dir
            else []
        )
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

        Every transformed mode returns the canonical *nested* row shape:
        each axis is a ``{code, label}`` cell (``time`` adds ``normalized`` /
        ``granularity``) and the observation is a ``{value, unit}`` measure.
        Call :meth:`StatsDataResponse.to_flat` for the one-column-per-field
        flat shape (pandas). ``rule`` selects the transformation mode:

        * ``"auto"`` (default) — classify the table's axes, then resolve a
          rule through Layers C > B > A > D: a matching v2 rule
          (user/project, then built-in), else a generic rule built from the
          classified roles (Layer A), else the Layer D fallback when the
          table cannot be structured (a low-confidence axis, or a shape the
          generic rule declines). A rule you supplied that then fails to apply
          surfaces as a typed :class:`EstatError`; a library-provided rule
          degrades to Layer D instead (ARCHITECTURE.md).
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
        # The pipeline owns the classify → aggregate → resolve → apply order
        # and the Layer A–D routing; this method keeps only HTTP, paging, and
        # response typing.
        from pyestat._engine.pipeline import run_pipeline

        transformed = run_pipeline(
            values,
            first.class_objs,
            first.table_inf,
            stats_data_id,
            rule,
            aggregates,
            user_rules=self._user_rules,
            project_rules=self._project_rules,
            builtin_rules=self._builtin_rules,
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
