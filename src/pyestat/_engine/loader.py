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
from pydantic import ValidationError

from pyestat._engine.role_defaults import expand_short_form
from pyestat._engine.rule import RuleV2
from pyestat.errors import RuleLoadError


# v2 is the only schema the engine speaks; the never-published v1 was
# retired. A file with any other ``schema_version`` fails fast at load time.
_SUPPORTED_VERSIONS = frozenset({"2"})


class YamlRuleLoader:
    """Loads rule files from disk.

    Stateless — instantiated for symmetry with future loaders that
    may carry migration tables, plugin registries, etc.
    """

    def load(self, path: Path) -> RuleV2:
        # Every failure mode here is wrapped in a typed RuleLoadError so a
        # malformed file surfaces as an EstatError, not a raw yaml / pydantic
        # / OSError — keeping the ``except EstatError`` contract whole for a
        # caller who dropped a bad file in their project rules directory.
        try:
            with path.open(encoding="utf-8") as f:
                data: Any = yaml.safe_load(f)
        except OSError as exc:
            raise RuleLoadError(path=path, reason=str(exc)) from exc
        except yaml.YAMLError as exc:
            raise RuleLoadError(path=path, reason=f"invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise RuleLoadError(
                path=path, reason="file must contain a mapping at the top level"
            )
        version = data.get("schema_version")
        if version not in _SUPPORTED_VERSIONS:
            raise RuleLoadError(
                path=path,
                reason=(
                    f"unsupported schema_version {version!r} "
                    f"(known: {sorted(_SUPPORTED_VERSIONS)})"
                ),
            )
        try:
            rule = RuleV2.model_validate(data)
        except ValidationError as exc:
            raise RuleLoadError(
                path=path, reason=f"schema validation failed: {exc}"
            ) from exc
        # Expand short form here so every caller downstream sees long form
        # (Done: "expanded at load time"). A short-form column that cannot be
        # expanded surfaces as RuleExpansionError (also an EstatError), so a
        # caller still catches any bad-rule-file with one ``except EstatError``.
        return expand_short_form(rule)

    def load_dir(self, path: Path) -> list[RuleV2]:
        """Load every ``*.yaml`` / ``*.yml`` file in ``path``, sorted by name.

        Returns an empty list when the directory is absent — the documented
        "no project-local rules" state, not an error. Only regular files are
        loaded: a sub-directory or dangling symlink whose name ends in
        ``.yaml`` is skipped rather than opened (which would raise an OS
        error). The extension match is case-insensitive, so ``.yml`` and
        ``.YAML`` are picked up too — the drop-in "place a file and it
        applies" contract should not silently ignore a common spelling.
        """
        if not path.is_dir():
            return []
        files = sorted(
            p
            for p in path.iterdir()
            if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
        )
        return [self.load(p) for p in files]
