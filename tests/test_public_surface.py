"""The public API surface pyestat promises at 0.x.

``__all__`` is the settled/evolving contract (README, docs/ARCHITECTURE.md):
the consumption path is settled, a few authoring handles are exported but
evolving, and the fine-grained authoring *leaf* errors are reachable only
through the private ``pyestat._errors`` module — so they carry no top-level
stability promise and may move during 0.x. The coarse ``except EstatError``
contract still covers every error, including the leaves kept off the top level.
"""
import importlib

import pytest

import pyestat

SETTLED_CONSUMPTION = {
    "EstatClient",
    "StatsDataResponse",
    "MetaInfoResponse",
    "StatsListResponse",
    "Page",
    "ClassObj",
    "EstatHttpClient",
    "ProgressEvent",
}
SETTLED_ERRORS = {
    "EstatError",
    "EstatApiError",
    "HttpRetryExhaustedError",
    "TooManyRowsError",
    "AmbiguousRuleError",
}
EVOLVING_HANDLES = {
    "RuleV2",
    "load_builtin_rules",
    "RuleAuthoringError",
    "TableExplanation",
    "AxisExplanation",
}

# Authoring leaf errors (and the rule-file load error) are deliberately kept
# off the top-level surface; they remain reachable via ``pyestat._errors``.
PRIVATE_ERROR_LEAVES = {
    "RoleResolutionError",
    "RuleExpansionError",
    "UnknownTransformError",
    "TimeFormatError",
    "FlatProjectionError",
    "RuleLoadError",
}


def test_all_is_the_settled_plus_evolving_surface():
    expected = SETTLED_CONSUMPTION | SETTLED_ERRORS | EVOLVING_HANDLES
    assert set(pyestat.__all__) == expected


def test_every_exported_name_resolves():
    for name in pyestat.__all__:
        assert hasattr(pyestat, name), name


def test_demoted_leaves_are_not_top_level():
    for name in PRIVATE_ERROR_LEAVES:
        assert name not in pyestat.__all__
        assert not hasattr(pyestat, name), name


def test_public_errors_module_path_is_gone():
    # The non-`_` ``pyestat.errors`` path is removed so it cannot become a
    # second, unmanaged public surface; only ``pyestat._errors`` remains.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pyestat.errors")
    importlib.import_module("pyestat._errors")


def test_demoted_leaves_reachable_via_private_module():
    mod = importlib.import_module("pyestat._errors")
    for name in PRIVATE_ERROR_LEAVES:
        assert hasattr(mod, name), name


def test_authoring_leaves_are_catchable_as_the_category():
    # A caller catches the whole evolving authoring category via the exported
    # ``RuleAuthoringError`` without importing each leaf.
    mod = importlib.import_module("pyestat._errors")
    for name in {"RoleResolutionError", "RuleExpansionError",
                 "UnknownTransformError", "TimeFormatError"}:
        assert issubclass(getattr(mod, name), pyestat.RuleAuthoringError), name


def test_every_error_still_inherits_estaterror():
    # The coarse ``except EstatError`` contract holds for the demoted leaves
    # (RuleLoadError included), even though they are no longer top-level.
    mod = importlib.import_module("pyestat._errors")
    for name in SETTLED_ERRORS | PRIVATE_ERROR_LEAVES:
        assert issubclass(getattr(mod, name), pyestat.EstatError), name
