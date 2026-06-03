"""Tests for the role-default registry and v2 transform registry (#22).

This is the Layer A substance: the named transforms a v2 rule may
reference, and the per-role defaults that fill a short-form rule's gaps.
The hard requirement carried over from the design discussion is that
the *defaults* are total functions — a rule built purely from them (a
Layer A generic rule) can never raise at apply time, so the auto path
always has output to return.
"""
from __future__ import annotations

from pyestat._engine.classifier import AxisRole
from pyestat._engine.role_defaults import (
    TRANSFORMS,
    default_transform,
)


class TestTransformRegistry:
    def test_minimum_registered_transforms(self) -> None:
        # Done scopes the registry to "passthrough + the existing time
        # parsers"; #4 adds iso8601 / jis_x_0401 later. Pinning the floor
        # so that expansion does not depend on transforms not yet shipped.
        names = set(TRANSFORMS.names())
        assert {"passthrough", "monthly_e_stat", "quarterly_e_stat", "yearly",
                "best_effort_time"} <= names

    def test_passthrough_returns_input_unchanged(self) -> None:
        assert TRANSFORMS.resolve("passthrough")("ＮＯ") == "ＮＯ"

    def test_named_time_parser_emits_normalized_string(self) -> None:
        # Decision 3: a v2 time column yields the normalized string only
        # (the v1 companion _code / _granularity columns are #29's job).
        assert TRANSFORMS.resolve("yearly")("2020000000") == "2020"
        assert TRANSFORMS.resolve("monthly_e_stat")("2020000505") == "2020-05"


class TestBestEffortTimeIsTotal:
    def test_recognised_codes_normalize(self) -> None:
        be = TRANSFORMS.resolve("best_effort_time")
        assert be("2020000505") == "2020-05"   # monthly
        assert be("2020000103") == "2020-Q1"   # quarterly
        assert be("2020000000") == "2020"      # yearly

    def test_unrecognised_code_is_returned_raw_not_raised(self) -> None:
        # Totality is the load-bearing property: the time role-default
        # never raises, so a Layer A generic rule applied to a weird code
        # degrades to the raw value instead of blowing up the request.
        be = TRANSFORMS.resolve("best_effort_time")
        assert be("not-a-date") == "not-a-date"

    def test_non_string_input_is_returned_raw_not_raised(self) -> None:
        # Totality must hold for non-string cells too. e-Stat codes arrive
        # as strings, but an int year (a JSON/YAML layer coercing a bare
        # 4-digit code, or a caller-built row) must degrade to its raw
        # value — never a TypeError out of the regex parsers, which would
        # break the Layer A "never lose structure" guarantee.
        be = TRANSFORMS.resolve("best_effort_time")
        assert be(2020) == 2020
        assert be(None) is None


class TestRoleDefaults:
    def test_time_defaults_to_best_effort(self) -> None:
        assert default_transform(AxisRole.TIME) == "best_effort_time"

    def test_other_roles_default_to_passthrough(self) -> None:
        for role in (AxisRole.AREA, AxisRole.VALUE, AxisRole.CATEGORY,
                     AxisRole.META_AXIS, AxisRole.UNKNOWN):
            assert default_transform(role) == "passthrough"

    def test_every_role_default_is_a_total_transform(self) -> None:
        # The Layer A safety guarantee, asserted structurally: every
        # role-default resolves to a transform that returns (does not
        # raise) on arbitrary, non-conforming input — string or not.
        for role in AxisRole:
            transform = TRANSFORMS.resolve(default_transform(role))
            for adversarial in ("???garbage???", 2020, None, 3.14):
                transform(adversarial)  # must not raise
