"""A rule that cannot be flattened is a routed authoring error, not a bare
crash — the nested form is always valid, and only ``to_flat`` is constrained.

``to_flat`` derives suffix columns (``{col}_label``, the bare ``unit`` a
``value`` measure carries, …) the rule's own unique-column check cannot see, so
two output columns can map to one flat key (a column ``unit`` beside a ``value``
measure). The nested form is unaffected; only the flat projection collides.

Business rule (ARCHITECTURE.md, "surface vs degrade"): such a collision is a
rule-authoring defect routed by provenance. A caller's rule keeps its valid
nested result and surfaces the typed error from ``to_flat`` (their names, their
fix); a library rule the caller cannot edit degrades to Layer D on the auto path
before they ever see it.
"""
from __future__ import annotations

import pytest

from pyestat._endpoint import ClassObj
from pyestat._engine.apply import apply_auto, apply_v2_rule
from pyestat._engine.canonical import measure, to_flat_rows
from pyestat._engine.classifier import (
    AxisClassification,
    AxisRole,
    Confidence,
    TableClassification,
)
from pyestat._engine.resolver import ResolvedRule, RuleLayer
from pyestat._engine.rule import RuleV2
from pyestat._errors import EstatError, FlatProjectionError, RuleAuthoringError


def _axis(axis_id: str, role: AxisRole) -> AxisClassification:
    return AxisClassification(axis_id, role, Confidence.HIGH, ("test",))


_CLASSIFICATION = TableClassification((
    _axis("time", AxisRole.TIME),
    _axis("area", AxisRole.AREA),
    _axis("tab", AxisRole.VALUE),
))

_ROWS = (
    {"time": "2020000000", "area": "13000", "tab": "020", "value": "123"},
    {"time": "2021000000", "area": "27000", "tab": "020", "value": "456"},
)


def _colliding_rule() -> RuleV2:
    # Column names are unique (passes the duplicate-name check), but the
    # ``value`` measure flattens its unit to the bare ``unit`` key, which the
    # dimension column literally named ``unit`` also claims — a flat collision.
    return RuleV2.model_validate({
        "schema_version": "2",
        "match": {"role_pattern": ["time", "area", "value"]},
        "output": [
            {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            {"column": "unit", "source": {"role": "area"}, "transform": "passthrough"},
        ],
    })


class TestFlatProjectionCollision:
    def test_to_flat_rows_raises_typed_error_on_collision(self) -> None:
        # The projection itself is the single source of collision truth: two
        # nested fields mapping to one flat key fail loud and typed (an
        # EstatError, not a bare ValueError), naming the colliding key.
        rows = (
            {"value": measure("123", "千円"), "unit": {"code": "13000", "label": "x"}},
        )
        with pytest.raises(FlatProjectionError) as exc:
            to_flat_rows(rows)
        assert exc.value.key == "unit"

    def test_nested_result_is_valid_and_surfaces_only_at_to_flat(self) -> None:
        # apply builds the nested result without rejecting it — each cell sits
        # under its own column, so a nested-only consumer is never penalized. The
        # collision is about flattening, so it surfaces only when to_flat runs.
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, _colliding_rule())
        assert out[0]["value"] == measure("123", None)      # nested cell is fine
        assert out[0]["unit"]["code"] == "13000"            # distinct nested key
        with pytest.raises(FlatProjectionError):
            to_flat_rows(out)

    def test_auto_caller_layer_keeps_result_surfaces_at_to_flat(self) -> None:
        # On rule="auto", a user/project rule the caller authored keeps its valid
        # nested result; the collision surfaces only if the caller flattens (their
        # names, their fix) — not eagerly at apply.
        resolved = ResolvedRule(_colliding_rule(), RuleLayer.USER)
        out = apply_auto(_ROWS, (), _CLASSIFICATION, resolved)
        assert out[0]["value"] == measure("123", None)      # nested result kept
        with pytest.raises(FlatProjectionError):
            to_flat_rows(out)

    def test_auto_library_layer_degrades_to_layer_d(self) -> None:
        # A library rule (builtin/generic) the caller cannot edit must not
        # surface: apply_auto detects the collision and degrades to lossless
        # Layer D, whose output is itself flat-safe.
        resolved = ResolvedRule(_colliding_rule(), RuleLayer.BUILTIN)
        out = apply_auto(_ROWS, (), _CLASSIFICATION, resolved)
        assert len(out) == len(_ROWS)              # Layer D is 1:1
        assert out[0]["value"] == measure("123", None)  # Layer D measure cell
        assert "unit" not in out[0]                # no colliding column survived
        to_flat_rows(out)                          # and it flattens cleanly

    def test_non_colliding_rule_flattens(self) -> None:
        # Regression: a well-named rule still projects to flat losslessly.
        rule = RuleV2.model_validate({
            "schema_version": "2",
            "match": {"role_pattern": ["time", "area", "value"]},
            "output": [
                {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        })
        out = apply_v2_rule(_ROWS, _CLASSIFICATION, rule)
        flat = to_flat_rows(out)
        assert flat[0] == {"area": "13000", "area_label": "13000",
                           "value": "123", "unit": None}


def test_flat_projection_error_is_a_routed_authoring_error() -> None:
    # It rides the RuleAuthoringError category (so the auto path routes it) and
    # the coarse EstatError contract.
    assert issubclass(FlatProjectionError, RuleAuthoringError)
    assert issubclass(FlatProjectionError, EstatError)
