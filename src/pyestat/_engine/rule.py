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
    """Selects one meta-axis member for a pivot column (#10).

    MVP supports equality against the member *name* (NFKC-normalized at
    apply time): an author writes the semantic label (e.g. ``"合計_金額"``),
    not the opaque, table-specific member code. Modeled as an object rather
    than a bare string so future selectors (member code, set membership)
    are additive and leave existing rules valid.
    """

    equals: str


class RoleSource(_Strict):
    """Where a v2 output column draws its value from: an axis *role*.

    ``role`` reuses the classifier's :class:`AxisRole` vocabulary so a
    rule and the classifier speak the same language. A ``where`` predicate
    turns a ``meta-axis`` role into a pivot (#10): rows are folded by the
    non-meta axes and the predicate picks which member's cell this column
    receives. ``where`` is valid only on a ``meta-axis`` source; on any
    other role a referenced role must resolve to exactly one axis.
    """

    role: AxisRole
    where: MetaWhere | None = None

    @model_validator(mode="after")
    def _where_requires_meta_axis(self) -> "RoleSource":
        if self.where is not None and self.role != AxisRole.META_AXIS:
            raise ValueError(
                "a `where` predicate is only valid on a meta-axis source "
                f"(got role={self.role.value})"
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
