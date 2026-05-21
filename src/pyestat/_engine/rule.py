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

from pydantic import BaseModel, ConfigDict


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
