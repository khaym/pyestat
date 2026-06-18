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

    def test_match_stats_code_is_optional_and_defaults_none(self) -> None:
        # The common case: a rule matches by role pattern alone, so the bundle
        # stays at O(role patterns). An absent stats_code is "any family".
        rule = RuleV2.model_validate(_LONG)
        assert rule.match.stats_code is None

    def test_match_stats_code_narrows_to_one_family(self) -> None:
        # A family-specific rule (#29) pins the e-Stat statsCode so a
        # structurally identical table from another survey does not match it;
        # role_pattern stays present as the matching authority.
        rule = RuleV2.model_validate(
            _long(match={"role_pattern": ["time", "area", "value"], "stats_code": "00350300"})
        )
        assert rule.match.stats_code == "00350300"
        assert rule.match.role_pattern == [AxisRole.TIME, AxisRole.AREA, AxisRole.VALUE]

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


class TestPivotWhereSchema:
    """The ``where`` predicate (#10) selects one meta-axis member for a
    pivot column. It is valid only on a ``meta-axis`` source, matches on the
    member *name* (the apply step NFKC-normalizes), and is modeled as an
    object so future selectors stay additive."""

    def test_where_predicate_parses_on_meta_axis_source(self) -> None:
        rule = RuleV2.model_validate(_long(
            match={"role_pattern": ["meta-axis", "time"]},
            output=[
                {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "amount_jpy",
                 "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}},
                 "transform": "passthrough"},
            ],
        ))
        source = rule.output[1].source
        assert source is not None
        assert source.role == AxisRole.META_AXIS
        assert source.where is not None
        assert source.where.equals == "合計_金額"

    def test_where_on_non_meta_axis_source_rejected(self) -> None:
        # A `where` on a time/area/value source is an authoring error: only
        # a meta-axis carries members to select among. Fail loud at load.
        with pytest.raises(ValidationError, match="meta-axis"):
            RuleV2.model_validate(_long(output=[
                {"column": "time",
                 "source": {"role": "time", "where": {"equals": "2020年"}},
                 "transform": "yearly"},
            ]))

    def test_unknown_where_field_rejected(self) -> None:
        # Same fail-loud stance as the rest of the schema: a misspelled
        # selector key must not silently widen the match to everything.
        with pytest.raises(ValidationError):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "x",
                     "source": {"role": "meta-axis", "where": {"eq": "合計_金額"}}},
                ],
            ))

    def test_parent_and_level_selectors_parse(self) -> None:
        # #37 widens `where` beyond name equality: a trade rule selects a
        # measure family by its parent member's name (合計_金額) and a depth
        # by @level — neither is expressible as an `equals`. Both coexist
        # with `equals` and are combined as AND when several are given.
        rule = RuleV2.model_validate(_long(
            match={"role_pattern": ["meta-axis", "time"]},
            output=[
                {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "amount",
                 "source": {"role": "meta-axis",
                            "where": {"parent": "合計_金額", "level": "2"}}},
            ],
        ))
        where = rule.output[1].source.where
        assert where is not None
        assert where.parent == "合計_金額"
        assert where.level == "2"
        assert where.equals is None

    def test_empty_where_predicate_rejected(self) -> None:
        # A `where: {}` with no selector would match every member (or none) —
        # an authoring slip that must fail loud, not silently pick.
        with pytest.raises(ValidationError, match="at least one"):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "x", "source": {"role": "meta-axis", "where": {}}},
                ],
            ))


class TestPivotKeySchema:
    """The ``key`` selector (#37) derives a grain dimension from a meta-axis
    member's name via a regex, so a measure×period cross folds without naming
    every member. Like ``where`` it is valid only on a ``meta-axis`` source,
    and a column is *either* a grain key *or* a value selector, never both."""

    def test_key_selector_parses_on_meta_axis_source(self) -> None:
        rule = RuleV2.model_validate(_long(
            match={"role_pattern": ["meta-axis", "time"]},
            output=[
                {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "month",
                 "source": {"role": "meta-axis", "key": {"pattern": r"^(\d{1,2}月)_"}}},
            ],
        ))
        source = rule.output[1].source
        assert source is not None
        assert source.key is not None
        assert source.key.pattern == r"^(\d{1,2}月)_"

    def test_key_on_non_meta_axis_source_rejected(self) -> None:
        # A grain key reads meta-axis member names; on a time/area source there
        # is nothing to pattern-match, so it is an authoring error.
        with pytest.raises(ValidationError, match="meta-axis"):
            RuleV2.model_validate(_long(output=[
                {"column": "time",
                 "source": {"role": "time", "key": {"pattern": "(.+)"}},
                 "transform": "yearly"},
            ]))

    def test_key_and_where_on_one_source_rejected(self) -> None:
        # The two have opposite jobs — key adds a grain row, where picks a
        # value within it — so one column cannot be both. Fail loud rather
        # than guess which wins.
        with pytest.raises(ValidationError, match="key.*where|where.*key"):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "x",
                     "source": {"role": "meta-axis",
                                "where": {"equals": "合計_金額"},
                                "key": {"pattern": "(.+)"}}},
                ],
            ))

    def test_unknown_key_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "x",
                     "source": {"role": "meta-axis", "key": {"regex": "(.+)"}}},
                ],
            ))

    def test_malformed_key_pattern_rejected_at_load(self) -> None:
        # A bad regex is an authoring error caught loud at load — the same
        # fail-fast stance as a misspelled field. Validating at the schema
        # keeps an uncompilable pattern from reaching the apply path, where a
        # raw re.error would dodge the auto path's typed-error routing.
        with pytest.raises(ValidationError, match="valid regex"):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "x",
                     "source": {"role": "meta-axis", "key": {"pattern": "(unbalanced"}}},
                ],
            ))


class TestPivotUnitFromSchema:
    """The ``unit_from`` selector (#39) fills the unit of the measure a
    ``where`` surfaces, reading it from a grain-less unit member (trade ships
    a quantity's unit as a level-1 member, not an ``@unit``). It reuses the
    ``where`` predicate vocabulary, is valid only on a ``meta-axis`` source,
    and modifies a ``where`` column — so it needs a ``where`` beside it."""

    def test_unit_from_parses_alongside_where(self) -> None:
        rule = RuleV2.model_validate(_long(
            match={"role_pattern": ["meta-axis", "time"]},
            output=[
                {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                {"column": "quantity",
                 "source": {"role": "meta-axis",
                            "where": {"parent": "合計_数量2"},
                            "unit_from": {"equals": "単位2"}}},
            ],
        ))
        source = rule.output[1].source
        assert source is not None
        assert source.unit_from is not None
        assert source.unit_from.equals == "単位2"

    def test_unit_from_on_non_meta_axis_source_rejected(self) -> None:
        # Only a meta-axis carries the unit member a `unit_from` reads. On a
        # time/area/value source there is nothing to select, so fail loud.
        with pytest.raises(ValidationError, match="meta-axis"):
            RuleV2.model_validate(_long(output=[
                {"column": "value",
                 "source": {"role": "value", "unit_from": {"equals": "単位2"}}},
            ]))

    def test_unit_from_without_a_where_rejected(self) -> None:
        # `unit_from` modifies the measure a `where` surfaces; with no `where`
        # there is no measure to attach a unit to, so the column is malformed.
        with pytest.raises(ValidationError, match="where"):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "quantity",
                     "source": {"role": "meta-axis", "unit_from": {"equals": "単位2"}}},
                ],
            ))

    def test_empty_unit_from_predicate_rejected(self) -> None:
        # `unit_from` reuses the `where` predicate, so an empty one (no
        # selector) is the same authoring slip — it would match everything.
        with pytest.raises(ValidationError, match="at least one"):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "quantity",
                     "source": {"role": "meta-axis",
                                "where": {"parent": "合計_数量2"}, "unit_from": {}}},
                ],
            ))


class TestAxisIdAddressing:
    """A source addresses an axis two ways (#38): by *role* (the default —
    resolves to the single axis of that role) or by *axis id* (``axis:``,
    picking one of several same-role axes). Id addressing is what lets a table
    with two ``category`` axes — 建築主 × 用途, 職種 × 企業規模 — map each to its
    own column; role addressing alone cannot tell them apart. Member selection
    *within* a meta-axis stays ``where`` / ``key``'s job, so ``axis`` is the
    companion for the non-meta repeated-role case."""

    def test_axis_addressed_source_parses(self) -> None:
        # A column may name both a role (its cell shape / transform default)
        # and the specific axis id it draws from.
        rule = RuleV2.model_validate(_long(
            match={"role_pattern": ["category", "category", "value"]},
            output=[
                {"column": "owner", "source": {"role": "category", "axis": "cat01"}},
                {"column": "use", "source": {"role": "category", "axis": "cat02"}},
                {"column": "value", "source": {"role": "value"}},
            ],
        ))
        assert rule.output[0].source is not None
        assert rule.output[0].source.role == AxisRole.CATEGORY
        assert rule.output[0].source.axis == "cat01"
        assert rule.output[1].source.axis == "cat02"
        # A source that names no axis keeps the role-only addressing.
        assert rule.output[2].source.axis is None

    def test_axis_on_a_value_source_rejected(self) -> None:
        # A VALUE column reads the observation cell, not an axis, so pinning an
        # axis id is meaningless — and apply never consults it (it resolves the
        # cell before axis resolution). Reject at load so the contract that "the
        # named axis is honored" holds for every role that accepts `axis`.
        with pytest.raises(ValidationError, match="directly-addressable"):
            RuleV2.model_validate(_long(output=[
                {"column": "v", "source": {"role": "value", "axis": "tab"}},
            ]))

    def test_axis_on_a_meta_axis_source_rejected(self) -> None:
        # A meta-axis selects *members* with where/key; `axis` picks *which*
        # axis a non-meta role draws from. Combining them on a meta-axis source
        # would be silently ignored (the pivot resolves the meta-axis by role),
        # so it is an authoring error caught loud.
        with pytest.raises(ValidationError, match="directly-addressable"):
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time", "source": {"role": "time"}, "transform": "yearly"},
                    {"column": "x",
                     "source": {"role": "meta-axis", "axis": "cat02",
                                "where": {"equals": "合計_金額"}}},
                ],
            ))

    def test_axis_parses_on_time_and_area_sources(self) -> None:
        # The non-meta, non-value roles all accept `axis` (a table can carry two
        # of any of them), not only category.
        rule = RuleV2.model_validate(_long(
            match={"role_pattern": ["time", "area", "value"]},
            output=[
                {"column": "t", "source": {"role": "time", "axis": "time"}, "transform": "yearly"},
                {"column": "a", "source": {"role": "area", "axis": "area"}, "transform": "passthrough"},
                {"column": "value", "source": {"role": "value"}, "transform": "passthrough"},
            ],
        ))
        assert rule.output[0].source.axis == "time"
        assert rule.output[1].source.axis == "area"

    def test_axis_survives_short_form_expansion(self) -> None:
        # Expansion fills the transform default but must preserve an explicit
        # axis — otherwise a repeated-role rule would lose the disambiguation.
        rule = expand_short_form(RuleV2.model_validate(_long(
            match={"role_pattern": ["category", "category", "value"]},
            output=[
                {"column": "owner", "source": {"role": "category", "axis": "cat01"}},
                {"column": "use", "source": {"role": "category", "axis": "cat02"}},
                {"column": "value", "source": {"role": "value"}},
            ],
        )))
        assert rule.output[0].source.axis == "cat01"
        assert rule.output[0].transform == "passthrough"
        assert rule.output[1].source.axis == "cat02"


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

    def test_meta_axis_where_column_keeps_where_and_defaults_passthrough(self) -> None:
        # A pivot column (#10) names a meta-axis source with a `where`
        # selector and usually omits the transform. Expansion must preserve
        # the predicate and fill the meta-axis role-default (passthrough) —
        # the selected member's cell is surfaced verbatim, not parsed.
        rule = expand_short_form(
            RuleV2.model_validate(_long(
                match={"role_pattern": ["meta-axis", "time"]},
                output=[
                    {"column": "time"},
                    {"column": "amount_jpy",
                     "source": {"role": "meta-axis", "where": {"equals": "合計_金額"}}},
                ],
            ))
        )
        amount = rule.output[1]
        assert amount.source is not None
        assert amount.source.role == AxisRole.META_AXIS
        assert amount.source.where is not None
        assert amount.source.where.equals == "合計_金額"
        assert amount.transform == "passthrough"

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
