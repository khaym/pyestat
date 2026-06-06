"""Tests for the v2 (output-schema-first) rule schema, its short-form
expansion, and the loader's version gate (task #22).

v2 is the only schema the engine speaks (#30 retired v1): the format
Layer B (built-in) and Layer C (project) rules are written in, and the
format the rule-authoring Skill (#8) generates. A v2 rule declares the
*output columns* the caller receives, not the input table structure —
so these tests pin the accepted long form, the short form sugar, and
exactly where a malformed rule fails loud.

The four-layer wiring that *selects* a v2 rule by role pattern is #28.
Here we only exercise the schema, the expansion, and the loader gate.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from pyestat._engine.classifier import AxisRole
from pyestat._engine.loader import YamlRuleLoader
from pyestat._engine.role_defaults import expand_short_form
from pyestat._engine.rule import RuleV2
from pyestat.errors import RuleExpansionError


# A minimum long-form rule: every column names its source role and
# transform explicitly. Reused with mutations below.
_LONG: dict = {
    "schema_version": "2",
    "match": {"role_pattern": ["time", "area", "value"]},
    "output": [
        {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
        {"column": "area", "source": {"role": "area"}, "transform": "passthrough"},
        {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
    ],
}


def _long(**overrides) -> dict:
    out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
           for k, v in _LONG.items()}
    out.update(overrides)
    return out


class TestRuleV2Schema:
    def test_long_form_parses(self) -> None:
        # The fully-explicit form is the canonical shape every short
        # form expands into; pinning it fixes the contract #28 applies
        # against and #8 generates.
        rule = RuleV2.model_validate(_LONG)
        assert rule.schema_version == "2"
        assert rule.match.role_pattern == [AxisRole.TIME, AxisRole.AREA, AxisRole.VALUE]
        assert rule.output[0].column == "time"
        assert rule.output[0].source is not None
        assert rule.output[0].source.role == AxisRole.TIME
        assert rule.output[0].transform == "yearly"

    def test_short_form_leaves_source_and_transform_unset(self) -> None:
        # The short form is sugar: a column may omit source and transform
        # and have them filled at load time. The raw model must accept
        # the gaps (expansion is a separate step, tested below).
        rule = RuleV2.model_validate(
            _long(output=[{"column": "time"}, {"column": "area"}, {"column": "value"}])
        )
        assert rule.output[0].source is None
        assert rule.output[0].transform is None

    def test_role_pattern_uses_the_classifier_vocabulary(self) -> None:
        # role_pattern is the same AxisRole vocabulary the classifier
        # emits; a value outside it is a rule-author error, not a new role.
        with pytest.raises(ValidationError):
            RuleV2.model_validate(_long(match={"role_pattern": ["time", "not_a_role"]}))

    def test_schema_version_must_be_2(self) -> None:
        with pytest.raises(ValidationError):
            RuleV2.model_validate(_long(schema_version="1"))

    def test_unknown_top_level_field_rejected(self) -> None:
        # Same fail-loud stance as v1: a misspelled section silently
        # disabling the rule sends the author debugging e-Stat instead.
        bad = _long()
        bad["outputs"] = bad.pop("output")
        with pytest.raises(ValidationError):
            RuleV2.model_validate(bad)

    def test_unknown_column_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuleV2.model_validate(
                _long(output=[{"column": "time", "transfrom": "yearly"}])
            )


class TestShortFormExpansion:
    def test_unspecified_transform_falls_back_to_role_default(self) -> None:
        # A column that names its source but omits transform inherits the
        # role-default: time → best-effort time parser, others → passthrough.
        rule = expand_short_form(
            RuleV2.model_validate(
                _long(output=[
                    {"column": "time", "source": {"role": "time"}},
                    {"column": "area", "source": {"role": "area"}},
                ])
            )
        )
        assert rule.output[0].transform == "best_effort_time"
        assert rule.output[1].transform == "passthrough"

    def test_unspecified_source_infers_role_from_column_name(self) -> None:
        # Fullest short form (#22 decision 1A): a bare column name doubles
        # as its role, so the canonical {time, area, value} table needs no
        # source/transform at all.
        rule = expand_short_form(
            RuleV2.model_validate(
                _long(output=[{"column": "time"}, {"column": "area"}, {"column": "value"}])
            )
        )
        assert rule.output[0].source is not None
        assert rule.output[0].source.role == AxisRole.TIME
        assert rule.output[0].transform == "best_effort_time"
        assert rule.output[2].source is not None
        assert rule.output[2].source.role == AxisRole.VALUE
        assert rule.output[2].transform == "passthrough"

    def test_column_name_that_is_not_a_role_needs_explicit_source(self) -> None:
        # The sugar only works when the column name is itself a role. A
        # column named "year" must spell out its source; omitting it is an
        # author error that fails loud at load — never a silent miss. This
        # bites only authoring/explicit contexts: the auto path keeps
        # Layer D as its escape hatch (#28).
        with pytest.raises(RuleExpansionError, match="year"):
            expand_short_form(
                RuleV2.model_validate(_long(output=[{"column": "year"}]))
            )

    def test_sentinel_and_pivot_roles_are_not_inferable_from_column_name(self) -> None:
        # AxisRole accepts "unknown" and "meta-axis" as enum values, but
        # neither is a directly addressable output source: "unknown" is a
        # classifier sentinel, and "meta-axis" needs a where-predicate
        # pivot (#10). A bare column so named must NOT silently bind to the
        # sentinel role — it is an authoring error that must spell out an
        # explicit source.
        for name in ("unknown", "meta-axis"):
            with pytest.raises(RuleExpansionError, match=name):
                expand_short_form(
                    RuleV2.model_validate(_long(output=[{"column": name}]))
                )

    def test_directly_addressable_roles_remain_inferable(self) -> None:
        # The narrowing above must not break the roles the short form is
        # for: time / area / value / category still infer from the name.
        rule = expand_short_form(
            RuleV2.model_validate(_long(
                match={"role_pattern": ["category"]},
                output=[{"column": "category"}],
            ))
        )
        assert rule.output[0].source is not None
        assert rule.output[0].source.role == AxisRole.CATEGORY

    def test_duplicate_output_column_names_rejected(self) -> None:
        # Two columns sharing a name would collapse at apply time (a dict
        # keeps only the last), silently dropping the earlier column's
        # data. That is an authoring error, caught loud at validation.
        with pytest.raises(ValidationError):
            RuleV2.model_validate(_long(output=[
                {"column": "v", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "v", "source": {"role": "value"}, "transform": "passthrough"},
            ]))

    def test_expansion_is_idempotent_on_long_form(self) -> None:
        # A fully-expanded rule expands to itself, so callers can expand
        # defensively without worrying whether the loader already did.
        once = expand_short_form(RuleV2.model_validate(_LONG))
        twice = expand_short_form(once)
        assert twice == once


class TestYamlRuleLoader:
    def _write(self, path: Path, body: str) -> Path:
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_loads_v2_and_expands_short_form(self, tmp_path: Path) -> None:
        # The loader owns short-form expansion (Done: "expanded at load
        # time"), so a caller reading a v2 file always gets long form.
        p = self._write(
            tmp_path / "generic.yaml",
            """
            schema_version: "2"
            match:
              role_pattern: [time, area, value]
            output:
              - column: time
              - column: area
              - column: value
            """,
        )
        rule = YamlRuleLoader().load(p)
        assert isinstance(rule, RuleV2)
        assert rule.output[0].source is not None
        assert rule.output[0].source.role == AxisRole.TIME
        assert rule.output[0].transform == "best_effort_time"

    def test_v1_file_fails_fast(self, tmp_path: Path) -> None:
        # #30 retired the never-published v1 schema: a leftover v1 file is
        # now an unknown version, so it fails loud at load time rather than
        # silently getting a stale interpretation.
        p = self._write(
            tmp_path / "legacy.yaml",
            """
            schema_version: "1"
            match: {statsCode: "00200524"}
            axes: {time: {id: time, format: monthly_e_stat}}
            value: {type: number}
            """,
        )
        with pytest.raises(ValueError, match="schema_version"):
            YamlRuleLoader().load(p)

    def test_unknown_version_still_rejected(self, tmp_path: Path) -> None:
        p = self._write(
            tmp_path / "future.yaml",
            """
            schema_version: "3"
            match: {role_pattern: [time]}
            output: [{column: time}]
            """,
        )
        with pytest.raises(ValueError, match="schema_version"):
            YamlRuleLoader().load(p)

    def test_non_mapping_top_level_rejected(self, tmp_path: Path) -> None:
        # A file whose top level is a list or scalar is a structural error;
        # it must fail loud with a clear message rather than an opaque
        # AttributeError when version gating calls ``data.get``.
        p = self._write(tmp_path / "list.yaml", "- not\n- a\n- mapping\n")
        with pytest.raises(ValueError, match="must contain a mapping"):
            YamlRuleLoader().load(p)

    def test_load_dir_returns_all_yaml_files_sorted(self, tmp_path: Path) -> None:
        # Sorted load order matters: the resolution chain reports ambiguity
        # by listing candidate rules, and the built-in loader relies on a
        # stable, diff-friendly order. The first output column's name is the
        # observable signal of which file loaded into which slot.
        self._write(
            tmp_path / "b.yaml",
            """
            schema_version: "2"
            match: {role_pattern: [value, time]}
            output: [{column: b_col, source: {role: value}, transform: passthrough}]
            """,
        )
        self._write(
            tmp_path / "a.yaml",
            """
            schema_version: "2"
            match: {role_pattern: [value, time]}
            output: [{column: a_col, source: {role: value}, transform: passthrough}]
            """,
        )
        rules = YamlRuleLoader().load_dir(tmp_path)
        assert [r.output[0].column for r in rules] == ["a_col", "b_col"]

    def test_load_dir_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        # The project-local "rules/" directory is optional; a consumer
        # without one must get "no project-local rules", not a
        # FileNotFoundError surfacing from an absent directory.
        assert YamlRuleLoader().load_dir(tmp_path / "does-not-exist") == []
