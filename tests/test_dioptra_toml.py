"""Always-on reconciliation: dioptra.toml declarations must match the code.

This guards the #1 failure mode of a Dioptra plugin — the manifest drifting
from the Python implementation (a task declared in TOML that no longer exists,
a param type referenced but never defined, or an entrypoint pointing at a
missing YAML). It requires no engine, so it always runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from atr_scanner import tasks


def _load_toml() -> dict:
    return tomllib.loads((REPO_ROOT / "dioptra.toml").read_text(encoding="utf-8"))


def _declared_task_names(toml: dict) -> set[str]:
    names: set[str] = set()
    for plugin in toml.get("plugins", []):
        for group in plugin.get("tasks", {}).values():
            for fn in group:
                names.add(fn["name"])
    return names


def _declared_param_types(toml: dict) -> set[str]:
    return {p["name"] for p in toml.get("plugin_param_types", [])}


def test_toml_parses():
    toml = _load_toml()
    assert toml.get("plugins"), "dioptra.toml has no [[plugins]]"


def test_declared_tasks_exist_in_code():
    toml = _load_toml()
    declared = _declared_task_names(toml)
    for name in declared:
        assert hasattr(tasks, name), (
            f"dioptra.toml declares task '{name}' but tasks.py has no such "
            f"function"
        )
        assert callable(getattr(tasks, name))


def test_expected_task_set_matches():
    """The TOML must declare exactly the tasks the code advertises as public."""
    toml = _load_toml()
    declared = _declared_task_names(toml)
    expected = {
        tasks.TASK_RESOLVE_INPUT,
        tasks.TASK_SCAN_MANIFESTS,
        tasks.TASK_WRITE_HITS_ARTIFACT,
        tasks.TASK_SUMMARIZE_HITS,
        tasks.TASK_ATR_SCAN_AND_REPORT,
    }
    assert declared == expected


def test_custom_param_types_are_defined_and_used():
    toml = _load_toml()
    defined = _declared_param_types(toml)
    # Our two custom types must be defined...
    assert tasks.PARAM_TYPE_HITS_TABLE in defined
    assert tasks.PARAM_TYPE_METRICS in defined

    # ...and every non-builtin type referenced by a task param must be defined.
    builtins = {"string", "bytes", "any", "boolean", "integer", "float", "number"}
    referenced: set[str] = set()
    for plugin in toml.get("plugins", []):
        for group in plugin.get("tasks", {}).values():
            for fn in group:
                for param in fn.get("input_params", []) + fn.get("output_params", []):
                    referenced.add(param["type"])
    unknown = referenced - defined - builtins
    assert not unknown, f"param types referenced but not defined: {unknown}"


def test_entrypoint_yaml_paths_exist():
    toml = _load_toml()
    for ep in toml.get("entrypoints", []):
        yaml_path = REPO_ROOT / ep["path"]
        assert yaml_path.exists(), f"entrypoint path missing: {ep['path']}"


def test_entrypoint_references_declared_plugin():
    toml = _load_toml()
    plugin_dirs = {Path(p["path"]).name for p in toml.get("plugins", [])}
    for ep in toml.get("entrypoints", []):
        for plugin_name in ep.get("plugins", []):
            assert plugin_name in plugin_dirs, (
                f"entrypoint references plugin '{plugin_name}' not declared in "
                f"[[plugins]]"
            )
