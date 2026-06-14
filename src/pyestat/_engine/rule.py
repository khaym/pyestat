"""Rule schema (Layer 3).

A rule is the versioned contract between rule files and the engine.
``schema_version`` is pinned at ``"2"``: the output-schema-first model
(:class:`RuleV2`) is the only schema the engine speaks. Additive
expansions (new optional fields, new transform keywords) keep the
version constant; breaking changes route through the loader's migration
step.

The schema deliberately leaves *name resolution* of transform-side
references (a column's ``transform`` name) to the application pipeline
rather than catching them at load time. This way a rule that ships at
the same time as a transform is added still loads cleanly on an old
library, and the error message when the transform is missing carries
table context.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from pyestat._engine.classifier import AxisRole


class _Strict(BaseModel):
    """Forbid unknown fields everywhere; rule-file typos are a major
    foot-gun otherwise (a misspelled ``match:`` silently disables the
    rule and the author debugs e-Stat instead of their YAML)."""

    model_config = ConfigDict(extra="forbid")


# --- v2: output-schema-first rules (PROPOSAL-AXIS-ROLE-INFERENCE, #22) ------
#
# v2 inverts v1: instead of describing the input axes, a rule declares the
# *output columns* the caller receives. Each column draws on an axis *role*
# (what the classifier inferred), not an axis id, so one rule covers every
# table sharing a role pattern. Short-form columns omit ``source`` /
# ``transform`` and are filled at load time from the role-default registry
# (see ``role_defaults.py``); the models below accept both forms, and
# expansion — not the schema — is what guarantees long form downstream.


class MetaWhere(_Strict):
    """Selects meta-axis members for a pivot column by their properties.

    A predicate over a member's metadata, combined as **AND** when several
    fields are given. All three are matched against signals an author can
    read in the metadata, never the opaque table-specific code:

    * ``equals`` — the member's own *name* (#10).
    * ``parent`` — its parent member's *name* (#37). The trade cross
      (``cat02``) groups months under a measure family (``合計_金額``); a
      family is selectable only by the parent's name, not by any one
      member's.
    * ``level`` — the member's ``@level`` depth, as a string.

    Names are NFKC-normalized at apply time, so an author writes the semantic
    label (``"合計_金額"``) regardless of width drift. At least one selector
    must be present — an empty predicate would match everything (or nothing)
    and is an authoring slip. As a pure filter ``where`` never changes the
    output grain; ``key`` does that.
    """

    equals: str | None = None
    parent: str | None = None
    level: str | None = None

    @model_validator(mode="after")
    def _at_least_one_selector(self) -> "MetaWhere":
        if self.equals is None and self.parent is None and self.level is None:
            raise ValueError(
                "a `where` predicate needs at least one selector "
                "(equals / parent / level)"
            )
        return self


class MetaKey(_Strict):
    """Derives a grain dimension from a meta-axis member's name (#37).

    ``pattern`` is a regex run against the member's NFKC-normalized name; its
    first capture group (or the whole match if it has none) becomes the
    column's value and **participates in the output grain** — the SQL analogue
    is a derived ``GROUP BY`` column. This is how a measure×period cross folds
    without enumerating members: the period (e.g. the month in ``"1月_金額"``)
    lives only in the name, not in any code, so a pattern lifts it into a
    row dimension that ``where`` columns are then resolved within.
    """

    pattern: str

    @model_validator(mode="after")
    def _pattern_compiles(self) -> "MetaKey":
        """A malformed regex is an authoring error, caught loud at load — the
        same fail-fast stance as a misspelled field. Validating here means the
        apply path never meets an uncompilable pattern, so a ``re.error`` can
        never escape it untyped (which would dodge the auto path's
        provenance routing — see ``apply._apply_pivot``)."""
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"`key.pattern` is not a valid regex: {exc}") from exc
        return self


class RoleSource(_Strict):
    """Where a v2 output column draws its value from: an axis *role*.

    ``role`` reuses the classifier's :class:`AxisRole` vocabulary so a
    rule and the classifier speak the same language. On a ``meta-axis`` role
    one of two pivot modifiers may appear (never both on one column — they
    have opposite jobs):

    * ``where`` — a filter (#10/#37): rows are folded by the non-meta axes
      (and any ``key`` grain) and the predicate picks which member's cell
      this column receives.
    * ``key`` — a grain dimension (#37): the column's value is derived from
      the member name and adds a row dimension to fold the cross around.
    * ``unit_from`` — a unit source (#39): a ``where``-style predicate that
      picks a *grain-less* meta member (trade ships a quantity's unit as a
      level-1 ``単位2`` member, its value the unit string) and folds that
      value into this measure's ``unit``. It modifies the measure ``where``
      surfaces, so it co-occurs with ``where`` and broadcasts to every
      period row.

    ``where`` and ``key`` are valid only on a ``meta-axis`` source; on any
    other role a referenced role must resolve to exactly one axis.
    """

    role: AxisRole
    where: MetaWhere | None = None
    key: MetaKey | None = None
    unit_from: MetaWhere | None = None

    @model_validator(mode="after")
    def _pivot_modifiers_require_meta_axis(self) -> "RoleSource":
        if self.where is not None and self.role != AxisRole.META_AXIS:
            raise ValueError(
                "a `where` predicate is only valid on a meta-axis source "
                f"(got role={self.role.value})"
            )
        if self.key is not None and self.role != AxisRole.META_AXIS:
            raise ValueError(
                "a `key` selector is only valid on a meta-axis source "
                f"(got role={self.role.value})"
            )
        if self.unit_from is not None and self.role != AxisRole.META_AXIS:
            raise ValueError(
                "a `unit_from` selector is only valid on a meta-axis source "
                f"(got role={self.role.value})"
            )
        if self.where is not None and self.key is not None:
            raise ValueError(
                "a column cannot carry both `where` and `key`: `where` selects "
                "a value, `key` derives a grain dimension — split them into two "
                "columns"
            )
        if self.unit_from is not None and self.where is None:
            raise ValueError(
                "`unit_from` fills the unit of the measure a `where` surfaces, "
                "so it needs a `where` on the same column (#39)"
            )
        return self


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

    The resolver (#28, ``resolve_v2``) compares this against the
    classifier's ``role_pattern``; axis ids never appear, which is what
    collapses the rule count from O(tables) to O(role patterns).
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
