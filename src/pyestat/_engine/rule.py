"""Rule schema (Layer 3).

The Rule model is the versioned contract between rule files and the
engine. ``schema_version`` is pinned at ``"1"`` for MVP; additive
expansions (new optional fields, new transformer keywords) keep the
version constant and breaking changes route through the loader's
migration step.

The schema deliberately leaves *name resolution* of transformer-side
references (``axes.time.format``, future ``axes.<id>.standard_code``)
to the matcher / transformer pipeline rather than catching them at
load time. This way a rule that ships at the same time as a parser
is added still loads cleanly on an old library, and the error message
when the parser is missing carries table context.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from pyestat._engine.classifier import AxisRole


class _Strict(BaseModel):
    """Forbid unknown fields everywhere; rule-file typos are a major
    foot-gun otherwise (a misspelled ``match:`` silently disables the
    rule and the author debugs e-Stat instead of their YAML)."""

    model_config = ConfigDict(extra="forbid")


class MatchRule(_Strict):
    """Narrowing predicates the matcher pipeline runs (Decision A).

    Only ``statsCode`` ships at MVP; the structural fingerprint is
    computed by the engine, not stored in the rule, so the rule file
    stays human-readable.
    """

    statsCode: str


class TimeAxisRule(_Strict):
    """Specifies which axis carries time semantics and how to parse it.

    ``format`` names a parser registered in
    :data:`pyestat._engine.time.TIME_PARSERS`. The string is not validated
    at load time — see the module docstring for the rationale.
    """

    id: str
    format: str


class AreaAxisRule(_Strict):
    """Specifies which axis carries area semantics.

    Optional in MVP (GDP has no area axis). ``format`` / ``standard_code``
    are deferred to the expansion list — see DESIGN.md Decision D.
    """

    id: str


class AxesRule(_Strict):
    time: TimeAxisRule
    area: AreaAxisRule | None = None


class ValueRule(_Strict):
    """Cell-value typing. Conditional typing (trade table) is deferred."""

    type: Literal["number", "string"]


class Rule(_Strict):
    """One bundled or user-supplied rule.

    The accompanying loader is responsible for ``schema_version``
    gating; this model only encodes the structure of version 1.
    """

    schema_version: Literal["1"]
    match: MatchRule
    axes: AxesRule
    value: ValueRule


# --- v2: output-schema-first rules (PROPOSAL-AXIS-ROLE-INFERENCE, #22) ------
#
# v2 inverts v1: instead of describing the input axes, a rule declares the
# *output columns* the caller receives. Each column draws on an axis *role*
# (what the classifier inferred), not an axis id, so one rule covers every
# table sharing a role pattern. Short-form columns omit ``source`` /
# ``transform`` and are filled at load time from the role-default registry
# (see ``role_defaults.py``); the models below accept both forms, and
# expansion — not the schema — is what guarantees long form downstream.


class RoleSource(_Strict):
    """Where a v2 output column draws its value from: an axis *role*.

    ``role`` reuses the classifier's :class:`AxisRole` vocabulary so a
    rule and the classifier speak the same language. The ``where``
    predicate that turns a multi-axis role into a pivot is deferred to
    #10; until then a referenced role must resolve to exactly one axis.
    """

    role: AxisRole


class OutputColumn(_Strict):
    """One declared output column.

    Long form sets all three fields. Short form gives only ``column``
    (its name doubling as the role) or ``column`` + ``source`` (letting
    the transform default); the omitted fields are ``None`` until
    expansion fills them.
    """

    column: str
    source: RoleSource | None = None
    transform: str | None = None


class MatchV2(_Strict):
    """v2 narrowing predicate: the ordered role pattern a table must show.

    The matcher (#28) compares this against the classifier's
    ``role_pattern``; axis ids never appear, which is what collapses the
    rule count from O(tables) to O(role patterns).
    """

    role_pattern: list[AxisRole]


class RuleV2(_Strict):
    """An output-schema-first rule (``schema_version: "2"``).

    Accepts both long and short forms; :func:`role_defaults.expand_short_form`
    normalizes a loaded rule to long form. The loader gates the version.
    """

    schema_version: Literal["2"]
    match: MatchV2
    output: list[OutputColumn]

    @model_validator(mode="after")
    def _reject_duplicate_columns(self) -> "RuleV2":
        """Output column names must be unique.

        Application builds each row as a dict keyed by ``column``; a
        repeated name would silently keep only the last writer and drop
        the earlier column's data. Caught here so the collision fails loud
        at load time rather than corrupting rows at request time.
        """
        seen: set[str] = set()
        dupes = {c.column for c in self.output if c.column in seen or seen.add(c.column)}
        if dupes:
            raise ValueError(f"duplicate output column name(s): {sorted(dupes)}")
        return self
