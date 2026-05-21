"""Tests for the Rule schema (Layer 3) and the YAML rule loader.

The schema is the contract that the rule-authoring Skill (task #8)
generates against, so every accepted shape and rejected shape is
pinned here: a silent schema drift would invalidate every existing
bundled and user-authored rule.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from pyestat._rule import Rule
from pyestat._rule_loader import YamlRuleLoader


# Common minimum-valid rule body, reused with mutations.
_MIN_RULE: dict = {
    "schema_version": "1",
    "match": {"statsCode": "00200524"},
    "axes": {"time": {"id": "time", "format": "monthly_e_stat"}},
    "value": {"type": "number"},
}


def _with(**overrides) -> dict:
    """Return a copy of the minimum rule with shallow-merged overrides."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in _MIN_RULE.items()}
    out.update(overrides)
    return out


class TestRuleMinimumSchema:
    def test_minimum_valid_rule_parses(self) -> None:
        # The MVP field set (Decision D) is intentionally tight; this
        # test pins the *floor* a rule must clear, so adding optional
        # fields later does not retroactively force them on rule authors.
        rule = Rule.model_validate(_MIN_RULE)
        assert rule.match.statsCode == "00200524"
        assert rule.axes.time.id == "time"
        assert rule.axes.time.format == "monthly_e_stat"
        assert rule.axes.area is None
        assert rule.value.type == "number"

    def test_value_type_must_be_number_or_string(self) -> None:
        # Decision D defers conditional value typing; the only two
        # accepted MVP values are "number" and "string". Pinning this
        # blocks a hopeful rule author from inventing "auto" before the
        # engine learns to handle it.
        with pytest.raises(ValidationError):
            Rule.model_validate(_with(value={"type": "auto"}))

    def test_optional_area_axis(self) -> None:
        # GDP has no area axis; the schema must let that through
        # without forcing a placeholder.
        rule = Rule.model_validate(_with(axes={"time": {"id": "time", "format": "yearly"}}))
        assert rule.axes.area is None

    def test_area_id_recognized_when_supplied(self) -> None:
        rule = Rule.model_validate(_with(axes={
            "time": {"id": "time", "format": "monthly_e_stat"},
            "area": {"id": "area"},
        }))
        assert rule.axes.area is not None
        assert rule.axes.area.id == "area"


class TestRuleStrictness:
    def test_unknown_top_level_field_rejected(self) -> None:
        # Typos like "matches:" instead of "match:" must fail loud at
        # load time; otherwise a misspelled rule would silently fall
        # back to "no rule matched" semantics and the author would
        # debug e-Stat instead of their rule file.
        bad = _with()
        bad["matches"] = bad.pop("match")
        with pytest.raises(ValidationError):
            Rule.model_validate(bad)

    def test_unknown_nested_field_rejected(self) -> None:
        bad = _with(match={"statsCode": "x", "exact_id": "0003443838"})
        with pytest.raises(ValidationError):
            Rule.model_validate(bad)

    def test_schema_version_is_required(self) -> None:
        # The version routes future migrations; the loader cannot apply
        # one if it never sees the field.
        bad = _with()
        del bad["schema_version"]
        with pytest.raises(ValidationError):
            Rule.model_validate(bad)


class TestYamlRuleLoader:
    def _write(self, path: Path, body: str) -> Path:
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_loads_yaml_file(self, tmp_path: Path) -> None:
        # YAML is the diff-reviewable rule format DESIGN.md picked;
        # the loader is the on-disk equivalent of model_validate.
        p = self._write(
            tmp_path / "population.yaml",
            """
            schema_version: "1"
            match:
              statsCode: "00200524"
            axes:
              time:
                id: time
                format: monthly_e_stat
            value:
              type: number
            """,
        )
        rule = YamlRuleLoader().load(p)
        assert rule.match.statsCode == "00200524"

    def test_load_dir_returns_all_yaml_files_sorted(self, tmp_path: Path) -> None:
        # Sorted load order matters because the resolution chain
        # (user > project > builtin) reports ambiguity by listing
        # candidate rules; a stable order keeps error messages stable.
        self._write(
            tmp_path / "b.yaml",
            """
            schema_version: "1"
            match: {statsCode: "B"}
            axes: {time: {id: time, format: yearly}}
            value: {type: number}
            """,
        )
        self._write(
            tmp_path / "a.yaml",
            """
            schema_version: "1"
            match: {statsCode: "A"}
            axes: {time: {id: time, format: yearly}}
            value: {type: number}
            """,
        )
        rules = YamlRuleLoader().load_dir(tmp_path)
        assert [r.match.statsCode for r in rules] == ["A", "B"]

    def test_load_dir_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        # The "project-local rules/" directory is optional; a consumer
        # without one must not see a FileNotFoundError surface from
        # what is, semantically, "I have no project-local rules".
        rules = YamlRuleLoader().load_dir(tmp_path / "does-not-exist")
        assert rules == []

    def test_rejects_unsupported_schema_version(self, tmp_path: Path) -> None:
        # Future-version files must fail with a clear message so the
        # rule author updates the library rather than silently getting
        # a stale interpretation.
        p = self._write(
            tmp_path / "future.yaml",
            """
            schema_version: "2"
            match: {statsCode: x}
            axes: {time: {id: time, format: yearly}}
            value: {type: number}
            """,
        )
        with pytest.raises(ValueError, match="schema_version"):
            YamlRuleLoader().load(p)

    def test_unknown_time_format_is_NOT_caught_at_load_time(self) -> None:
        # The loader's job is structural; format name resolution is
        # the matcher pipeline's job (so a rule referencing an unknown
        # format is reported with table context at match time, not
        # at startup before any table has been touched). Pinning this
        # boundary so refactors do not accidentally tighten it.
        rule = Rule.model_validate(_with(axes={"time": {"id": "time", "format": "not_a_parser"}}))
        assert rule.axes.time.format == "not_a_parser"
