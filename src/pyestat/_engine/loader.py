"""YAML rule loader (Layer 3).

Reads ``.yaml`` rule files into :class:`RuleV2` instances. The loader
owns ``schema_version`` gating so a future migration step can sit
between the raw mapping and the pydantic validator without every
caller learning about versions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pyestat._engine.role_defaults import expand_short_form
from pyestat._engine.rule import RuleV2


# v2 is the only schema the engine speaks (#30 retired the never-published
# v1). A file with any other ``schema_version`` fails fast at load time.
_SUPPORTED_VERSIONS = frozenset({"2"})


class YamlRuleLoader:
    """Loads rule files from disk.

    Stateless — instantiated for symmetry with future loaders that
    may carry migration tables, plugin registries, etc.
    """

    def load(self, path: Path) -> RuleV2:
        with path.open(encoding="utf-8") as f:
            data: Any = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"rule file {path} must contain a mapping at the top level"
            )
        version = data.get("schema_version")
        if version not in _SUPPORTED_VERSIONS:
            raise ValueError(
                f"unsupported schema_version {version!r} in {path} "
                f"(known: {sorted(_SUPPORTED_VERSIONS)})"
            )
        # Expand short form here so every caller downstream sees long form
        # (Done: "expanded at load time").
        return expand_short_form(RuleV2.model_validate(data))

    def load_dir(self, path: Path) -> list[RuleV2]:
        """Load every ``*.yaml`` file in ``path`` in sorted order.

        Returns an empty list when the directory is absent — that is
        the documented "no project-local rules" state, not an error.
        """
        if not path.is_dir():
            return []
        return [self.load(p) for p in sorted(path.glob("*.yaml"))]
