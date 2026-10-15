"""Compatibility checks against an installed Dioptra (``dioptra-platform``).

Skipped when Dioptra is not installed. In CI this runs against the pinned
Dioptra release, so a Dioptra change that would break the import or the job
(schema, builtin types, plugin discovery, artifact interface) fails here before
anyone imports the plugin into a live deployment.
"""

from __future__ import annotations

import importlib
import json
import sys
from importlib import resources
from pathlib import Path

import pytest

pytest.importorskip("dioptra", reason="dioptra-platform not installed")
jsonschema = pytest.importorskip("jsonschema")

from dioptra import pyplugs  # noqa: E402
from dioptra.sdk.api.artifact import ArtifactTaskInterface  # noqa: E402
from dioptra.task_engine.type_registry import BUILTIN_TYPES  # noqa: E402

import test_dioptra_toml as reconciliation  # noqa: E402

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent


def _toml() -> dict:
    return tomllib.loads((REPO_ROOT / "dioptra.toml").read_text(encoding="utf-8"))


def test_dioptra_toml_passes_dioptras_import_schema():
    schema_file = resources.files("dioptra.restapi.v1.workflows").joinpath(
        "dioptra-resources.schema.json"
    )
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    jsonschema.validate(_toml(), schema)


def test_mirrored_constants_match_installed_dioptra():
    from dioptra.restapi.v1.plugins.schema import ALLOWED_PLUGIN_FILENAME_REGEX
    from dioptra.restapi.v1.workflows.service import VALID_ENTRYPOINT_PARAM_TYPES

    assert set(BUILTIN_TYPES) == reconciliation.DIOPTRA_BUILTIN_TYPES
    assert set(VALID_ENTRYPOINT_PARAM_TYPES) == reconciliation.DIOPTRA_ENTRYPOINT_PARAM_TYPES
    assert ALLOWED_PLUGIN_FILENAME_REGEX.pattern == reconciliation.DIOPTRA_PLUGIN_FILENAME.pattern


def test_function_tasks_are_registered_with_pyplugs():
    """The task engine calls tasks via pyplugs.call(package, module, name)."""
    for plugin in _toml()["plugins"]:
        package = Path(plugin["path"]).name
        for task in plugin.get("tasks", {}).get("functions", []):
            module = Path(task["filename"]).stem
            importlib.import_module(f"{package}.{module}")
            info = pyplugs.info(package, module, task["name"])
            assert info.func_name == task["name"]


def test_artifact_tasks_implement_dioptras_interface():
    """Dioptra skips artifact tasks that do not subclass ArtifactTaskInterface."""
    for plugin in _toml()["plugins"]:
        package = Path(plugin["path"]).name
        for task in plugin.get("tasks", {}).get("artifacts", []):
            module = importlib.import_module(f"{package}.{Path(task['filename']).stem}")
            assert issubclass(getattr(module, task["name"]), ArtifactTaskInterface)
