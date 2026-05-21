"""YAML rule loader (Layer 3).

Reads ``.yaml`` rule files into :class:`Rule` instances. The loader
owns ``schema_version`` gating so a future migration step can sit
between the raw mapping and the pydantic validator without every
caller learning about versions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pyestat._rule import Rule


_SUPPORTED_VERSIONS = frozenset({"1"})


class YamlRuleLoader:
    """Loads rule files from disk.

    Stateless — instantiated for symmetry with future loaders that
    may carry migration tables, plugin registries, etc.
    """

    def load(self, path: Path) -> Rule:
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
        return Rule.model_validate(data)

    def load_dir(self, path: Path) -> list[Rule]:
        """Load every ``*.yaml`` file in ``path`` in sorted order.

        Returns an empty list when the directory is absent — that is
        the documented "no project-local rules" state, not an error.
        """
        if not path.is_dir():
            return []
        return [self.load(p) for p in sorted(path.glob("*.yaml"))]
