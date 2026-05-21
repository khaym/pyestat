"""Transformer pipeline (Layer 3 sub-component).

Each :class:`Transformer` is a stream-in / stream-out generator so a
3.8M-row table never materializes fully in memory. The pipeline
composition is built by the engine at rule-load time: it walks the
:class:`Rule` and appends the relevant Transformer instances; adding
a new Transformer (``AggregateExcluder``, ``StandardCodeMapper`` …)
is a three-step process and never touches existing Transformers.

The :class:`TransformContext` carries the side-information the
bundled Transformers need today. New Transformers add defaulted
fields here so existing ones stay untouched (ARCHITECTURE.md).
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pyestat._engine.rule import Rule
from pyestat._engine.time import TIME_PARSERS


Row = Mapping[str, Any]


@dataclass(frozen=True)
class TransformContext:
    """Per-call context handed to every Transformer in the pipeline."""

    stats_data_id: str
    class_inf: Mapping[str, tuple[dict[str, Any], ...]]
    axes_meta: Mapping[str, str]


class Transformer(Protocol):
    def transform(
        self,
        rows: Iterator[Row],
        rule: Rule,
        ctx: TransformContext,
    ) -> Iterator[Row]: ...


class TimeNormalizer:
    """Adds ``time`` (normalized) / ``time_granularity`` (tag) to each row.

    The raw code is preserved under ``{axis_id}_code`` (typically
    ``time_code``) so callers who want the original e-Stat code can
    still get at it without refetching.
    """

    def transform(
        self,
        rows: Iterator[Row],
        rule: Rule,
        ctx: TransformContext,
    ) -> Iterator[Row]:
        parser = TIME_PARSERS.resolve(rule.axes.time.format)
        axis_id = rule.axes.time.id
        code_key = f"{axis_id}_code"
        for row in rows:
            code = row.get(axis_id)
            if code is None:
                yield row
                continue
            point = parser(code)
            yield {
                **row,
                axis_id: point.normalized,
                code_key: code,
                "time_granularity": point.granularity,
            }


class ValueCaster:
    """Coerces the ``value`` field per ``rule.value.type``.

    Marker strings ("-" for missing, "***" for confidential, "") that
    cannot be coerced to a number are passed through unchanged so the
    caller can distinguish "no data" from "zero" — coercing them would
    erase that distinction.
    """

    def transform(
        self,
        rows: Iterator[Row],
        rule: Rule,
        ctx: TransformContext,
    ) -> Iterator[Row]:
        target = rule.value.type
        if target == "string":
            for row in rows:
                if "value" in row:
                    yield {**row, "value": str(row["value"])}
                else:
                    yield row
            return
        for row in rows:
            raw = row.get("value")
            if raw is None or raw == "":
                yield row
                continue
            try:
                f = float(raw)
            except (TypeError, ValueError):
                yield row
                continue
            yield {**row, "value": int(f) if f.is_integer() else f}
