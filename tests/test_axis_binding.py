"""Tests for resolving an output column to a concrete axis (#38, #40).

``_bind_axis`` owns the addressing rule the engine had split across role- and
id-based paths: a column names an axis id, or — naming none — falls back to the
role's single axis. The rule decides which axis each column reads *before* any
row is touched, so a repeated or absent role fails identifiably (the auto path
then routes to Layer D) rather than reading every row's missing cell as
``None``. These tests exercise that rule in isolation.
"""
from __future__ import annotations

import pytest

from pyestat._engine.apply import AxisBinding, _bind_axis
from pyestat._engine.classifier import AxisRole
from pyestat._engine.rule import OutputColumn
from pyestat.errors import RoleResolutionError


def _col(column: str, role: str, axis: str | None = None, transform: str = "passthrough") -> OutputColumn:
    source: dict[str, str] = {"role": role}
    if axis is not None:
        source["axis"] = axis
    return OutputColumn.model_validate(
        {"column": column, "source": source, "transform": transform}
    )


# role → axes, the shape `apply_v2_rule` derives from a classification. Two
# `category` axes model a 建築主 × 用途 cross, where role alone is ambiguous.
_TWO_CATEGORIES = {
    AxisRole.CATEGORY: ["cat01", "cat02"],
    AxisRole.TIME: ["time"],
}


class TestBindAxis:
    def test_value_reads_the_observation_cell_so_binds_no_axis(self) -> None:
        binding = _bind_axis(_col("v", "value"), {})
        assert binding == AxisBinding("v", AxisRole.VALUE, None, "passthrough")

    def test_role_only_resolves_to_the_single_axis_of_that_role(self) -> None:
        binding = _bind_axis(
            _col("t", "time", transform="best_effort_time"), {AxisRole.TIME: ["time"]}
        )
        assert binding.axis_id == "time"

    def test_role_only_with_a_repeated_role_is_unresolvable(self) -> None:
        with pytest.raises(RoleResolutionError, match="multiple axes"):
            _bind_axis(_col("c", "category"), _TWO_CATEGORIES)

    def test_a_role_absent_from_the_table_is_unresolvable(self) -> None:
        with pytest.raises(RoleResolutionError, match="no axis is classified"):
            _bind_axis(_col("a", "area"), {AxisRole.TIME: ["time"]})

    def test_an_axis_id_picks_one_among_several_same_role_axes(self) -> None:
        binding = _bind_axis(_col("c", "category", axis="cat02"), _TWO_CATEGORIES)
        assert binding.axis_id == "cat02"

    def test_an_axis_id_not_carrying_the_role_is_rejected(self) -> None:
        with pytest.raises(RoleResolutionError, match="not classified as"):
            _bind_axis(_col("c", "category", axis="time"), _TWO_CATEGORIES)

    def test_a_bare_meta_axis_cannot_bind_to_one_member(self) -> None:
        with pytest.raises(RoleResolutionError, match="needs a `where`"):
            _bind_axis(_col("m", "meta-axis"), {AxisRole.META_AXIS: ["cat01"]})
