"""Always-on reconciliation: dioptra.toml declarations must match the code.

This guards the #1 failure mode of a Dioptra plugin — the manifest drifting
from the Python implementation (a task declared in TOML that no longer exists,
a param type referenced but never defined, an entrypoint pointing at a missing
YAML, or a graph step calling a task the entrypoint cannot see). It requires no
engine and no Dioptra install, so it always runs. The constants below mirror
Dioptra 1.2.1's resource-import rules; tests/test_dioptra_compat.py checks the
same file against the installed Dioptra itself when it is available.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from atr_scanner import tasks

# dioptra.task_engine.type_registry.BUILTIN_TYPES (Dioptra 1.2.1)
DIOPTRA_BUILTIN_TYPES = {"any", "string", "integer", "number", "boolean", "null"}
# dioptra.restapi.v1.workflows.service.VALID_ENTRYPOINT_PARAM_TYPES (1.2.1)
DIOPTRA_ENTRYPOINT_PARAM_TYPES = {
    "string",
    "float",
    "integer",
    "boolean",
    "list",
    "mapping",
}
# dioptra.restapi.v1.plugins.schema.ALLOWED_PLUGIN_FILENAME_REGEX (1.2.1)
DIOPTRA_PLUGIN_FILENAME = re.compile(
    r"^(?!.*/_)(?!_/)([a-zA-Z][a-zA-Z0-9_]*/)*[a-zA-Z_][a-zA-Z0-9_]*\.py$"
)
# Graph references: "$name" or "$step.output".
_REFERENCE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?$")


def _load_toml() -> dict:
    return tomllib.loads((REPO_ROOT / "dioptra.toml").read_text(encoding="utf-8"))


def _plugins_by_name(toml: dict) -> dict[str, dict]:
    return {Path(p["path"]).name: p for p in toml.get("plugins", [])}


def _tasks_of(plugin: dict, kind: str) -> list[dict]:
    return plugin.get("tasks", {}).get(kind, [])


def _import_plugin_file(plugin: dict, filename: str):
    module = f"{Path(plugin['path']).name}.{Path(filename).stem}"
    return importlib.import_module(module)


def _declared_param_types(toml: dict) -> set[str]:
    return {p["name"] for p in toml.get("plugin_param_types", [])}


def _references(value) -> list[str]:
    """Collect every $reference inside a graph/artifact node."""
    if isinstance(value, str):
        return [value] if value.startswith("$") else []
    if isinstance(value, dict):
        return [ref for v in value.values() for ref in _references(v)]
    if isinstance(value, list):
        return [ref for v in value for ref in _references(v)]
    return []


def test_toml_parses():
    toml = _load_toml()
    assert toml.get("plugins"), "dioptra.toml has no [[plugins]]"


def test_declared_function_tasks_exist_in_code():
    for plugin in _load_toml()["plugins"]:
        for task in _tasks_of(plugin, "functions"):
            module = _import_plugin_file(plugin, task["filename"])
            func = getattr(module, task["name"], None)
            assert callable(func), (
                f"dioptra.toml declares task '{task['name']}' but "
                f"{plugin['path']}/{task['filename']} has no such function"
            )


def test_declared_artifact_tasks_exist_in_code():
    for plugin in _load_toml()["plugins"]:
        for task in _tasks_of(plugin, "artifacts"):
            module = _import_plugin_file(plugin, task["filename"])
            cls = getattr(module, task["name"], None)
            assert cls is not None, f"artifact task '{task['name']}' missing"
            for method in ("serialize", "deserialize", "validation"):
                assert callable(getattr(cls, method, None)), (
                    f"artifact task '{task['name']}' lacks {method}()"
                )


def test_expected_task_set_matches():
    """The TOML must declare exactly the tasks the code advertises as public."""
    plugin = _plugins_by_name(_load_toml())["atr_scanner"]
    declared = {task["name"] for task in _tasks_of(plugin, "functions")}
    expected = {
        tasks.TASK_RESOLVE_INPUT,
        tasks.TASK_SCAN_MANIFESTS,
        tasks.TASK_WRITE_HITS_ARTIFACT,
        tasks.TASK_SUMMARIZE_HITS,
        tasks.TASK_LOG_SCAN_METRICS,
        tasks.TASK_ATR_SCAN_AND_REPORT,
    }
    assert declared == expected


def test_custom_param_types_are_defined_and_used():
    toml = _load_toml()
    defined = _declared_param_types(toml)
    # Our two custom types must be defined...
    assert tasks.PARAM_TYPE_HITS_TABLE in defined
    assert tasks.PARAM_TYPE_METRICS in defined
    # ...must not shadow a Dioptra builtin...
    assert not defined & DIOPTRA_BUILTIN_TYPES

    # ...and every non-builtin type referenced by a task param must be defined.
    referenced: set[str] = set()
    for plugin in toml.get("plugins", []):
        for group in plugin.get("tasks", {}).values():
            for fn in group:
                for param in fn.get("input_params", []) + fn.get("output_params", []):
                    referenced.add(param["type"])
    unknown = referenced - defined - DIOPTRA_BUILTIN_TYPES
    assert not unknown, f"param types referenced but not defined: {unknown}"


def test_plugin_files_have_importable_names():
    for plugin in _load_toml()["plugins"]:
        root = REPO_ROOT / plugin["path"]
        assert root.is_dir(), f"plugin path is not a directory: {plugin['path']}"
        for path in root.rglob("[!.]*.py"):
            name = path.relative_to(root).as_posix()
            if "__pycache__" in name:
                continue
            assert DIOPTRA_PLUGIN_FILENAME.fullmatch(name), (
                f"Dioptra would reject plugin file name: {plugin['path']}/{name}"
            )


def test_entrypoint_yaml_paths_exist():
    toml = _load_toml()
    for ep in toml.get("entrypoints", []):
        yaml_path = REPO_ROOT / ep["path"]
        assert yaml_path.exists(), f"entrypoint path missing: {ep['path']}"


def test_entrypoint_references_declared_plugins():
    toml = _load_toml()
    plugins = _plugins_by_name(toml)
    for ep in toml.get("entrypoints", []):
        for plugin_name in ep.get("plugins", []) + ep.get("artifact_plugins", []):
            assert plugin_name in plugins, (
                f"entrypoint references plugin '{plugin_name}' not declared in "
                f"[[plugins]]"
            )


def test_entrypoint_param_types_are_valid():
    for ep in _load_toml().get("entrypoints", []):
        for param in ep.get("params", []):
            assert param["type"] in DIOPTRA_ENTRYPOINT_PARAM_TYPES, param


@pytest.mark.parametrize(
    "entrypoint", _load_toml().get("entrypoints", []), ids=lambda ep: ep["name"]
)
def test_entrypoint_graph_matches_declarations(entrypoint):
    """Every step calls a task the entrypoint can see, with declared inputs,
    and every $reference names an entrypoint param or an earlier step."""
    toml = _load_toml()
    plugins = _plugins_by_name(toml)
    graph_file = yaml.safe_load((REPO_ROOT / entrypoint["path"]).read_text())

    function_tasks = {
        task["name"]: task
        for name in entrypoint.get("plugins", [])
        for task in _tasks_of(plugins[name], "functions")
    }
    artifact_tasks = {
        task["name"]
        for name in entrypoint.get("artifact_plugins", [])
        for task in _tasks_of(plugins[name], "artifacts")
    }
    params = {p["name"] for p in entrypoint.get("params", [])}
    graph = graph_file.get("graph") or {}
    assert graph, f"{entrypoint['path']} has no graph"

    for step_name, step in graph.items():
        assert len(step) == 1, f"step {step_name} must call exactly one task"
        (task_name, args), = step.items()
        assert task_name in function_tasks, (
            f"step {step_name} calls '{task_name}', which is not declared by the "
            f"entrypoint's plugins"
        )
        declared_inputs = {p["name"] for p in function_tasks[task_name]["input_params"]}
        required = {
            p["name"]
            for p in function_tasks[task_name]["input_params"]
            if p.get("required")
        }
        assert set(args) <= declared_inputs, (step_name, set(args) - declared_inputs)
        assert required <= set(args), (step_name, required - set(args))

    targets = params | set(graph)
    for node in list(graph.values()) + list((graph_file.get("artifacts") or {}).values()):
        for ref in _references(node):
            match = _REFERENCE.match(ref)
            assert match and match.group(1) in targets, f"dangling reference {ref}"

    for name, artifact in (graph_file.get("artifacts") or {}).items():
        assert artifact["task"]["name"] in artifact_tasks, (
            f"artifact '{name}' uses task '{artifact['task']['name']}', which is "
            f"not declared by the entrypoint's artifact_plugins"
        )


def test_hits_table_columns_agree_across_plugins():
    from atr_artifacts.tasks import HITS_TABLE_COLUMNS

    assert tuple(tasks._CSV_COLUMNS) == HITS_TABLE_COLUMNS
