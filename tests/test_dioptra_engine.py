"""Run the entrypoint through Dioptra's own task engine, without a deployment.

Builds the job configuration the way Dioptra's REST API does from the
imported resources (tasks, types, parameters, graph, artifact outputs), then
uses Dioptra's job runner pieces to validate it, discover the artifact task,
and execute the graph with ``run_experiment`` on the bundled samples. The only
stand-in is the worker's REST client, which records the metric calls after
checking them against the real client signature.

Needs both ``dioptra-platform`` and ``pyatr``; skipped otherwise.
"""

from __future__ import annotations

import copy
import inspect
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("dioptra", reason="dioptra-platform not installed")
pytest.importorskip("pyatr", reason="pyatr not installed")

import structlog  # noqa: E402
from dioptra.client import jobs as client_jobs  # noqa: E402
from dioptra.restapi.v1.entrypoints.task_engine_yaml import (  # noqa: E402
    _ENTRYPOINT_TO_TASK_ENGINE_TYPES,
)
from dioptra.sdk.utilities import auth_client  # noqa: E402
from dioptra.sdk.utilities import run_dioptra_job as job_runner  # noqa: E402
from dioptra.sdk.utilities.contexts import env_vars  # noqa: E402
from dioptra.sdk.utilities.entrypoint_swaps import compile_swaps_config  # noqa: E402
from dioptra.task_engine.task_engine import run_experiment  # noqa: E402
from dioptra.task_engine.type_registry import BUILTIN_TYPES  # noqa: E402

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "examples" / "sample_manifests"


def _task_entry(package: str, task: dict) -> dict:
    entry: dict = {"plugin": f"{package}.{Path(task['filename']).stem}.{task['name']}"}
    if task["input_params"]:
        entry["inputs"] = [
            {"name": p["name"], "type": p["type"], "required": p.get("required", False)}
            for p in task["input_params"]
        ]
    outputs = task["output_params"]
    if len(outputs) == 1:
        entry["outputs"] = {outputs[0]["name"]: outputs[0]["type"]}
    elif outputs:
        entry["outputs"] = [{o["name"]: o["type"]} for o in outputs]
    return entry


def _job_config(toml: dict, entrypoint: dict) -> dict:
    """Mirror dioptra.restapi.v1.entrypoints.task_engine_yaml for this repo."""
    graph_file = yaml.safe_load((REPO_ROOT / entrypoint["path"]).read_text())
    tasks = {
        task["name"]: _task_entry(Path(plugin["path"]).name, task)
        for plugin in toml["plugins"]
        if Path(plugin["path"]).name in entrypoint["plugins"]
        for task in plugin.get("tasks", {}).get("functions", [])
    }
    types = {
        p["name"]: None for p in toml["plugin_param_types"] if p["name"] not in BUILTIN_TYPES
    }
    referenced = {
        value[1:].split(".")[0]
        for step in graph_file["graph"].values()
        for args in step.values()
        for value in args.values()
        if isinstance(value, str) and value.startswith("$")
    }
    parameters = {
        p["name"]: {
            "default": p["default_value"],
            "type": _ENTRYPOINT_TO_TASK_ENGINE_TYPES[p["type"]],
        }
        for p in entrypoint["params"]
        if p["name"] in referenced
    }
    return {
        "types": types,
        "parameters": parameters,
        "tasks": tasks,
        "graph": graph_file["graph"],
        "artifact_outputs": graph_file["artifacts"],
    }


def _artifact_plugins(toml: dict, entrypoint: dict) -> list[dict]:
    """The shape run_dioptra_job reads from get_artifact_plugins()."""
    plugins = []
    for snapshot_id, plugin in enumerate(toml["plugins"], start=1):
        name = Path(plugin["path"]).name
        if name not in entrypoint.get("artifact_plugins", []):
            continue
        declared = plugin.get("tasks", {}).get("artifacts", [])
        files = [
            {
                "filename": path.name,
                "tasks": {
                    "artifacts": [
                        {"name": t["name"], "id": index}
                        for index, t in enumerate(declared, start=1)
                        if t["filename"] == path.name
                    ]
                },
            }
            for path in sorted((REPO_ROOT / plugin["path"]).glob("*.py"))
        ]
        plugins.append({"name": name, "snapshotId": snapshot_id, "files": files})
    return plugins


class _RecordingJobs:
    """Worker client stand-in; rejects calls the real client would not accept."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._signature = inspect.signature(
            client_jobs.JobsCollectionClient.append_metric_by_id
        )

    def append_metric_by_id(self, **kwargs) -> None:
        self._signature.bind(None, **kwargs)
        self.calls.append(kwargs)


@pytest.fixture
def engine_run(tmp_path, monkeypatch):
    toml = tomllib.loads((REPO_ROOT / "dioptra.toml").read_text(encoding="utf-8"))
    entrypoint = toml["entrypoints"][0]
    monkeypatch.chdir(tmp_path)
    context = job_runner.Context()
    context.mkdirs()
    for plugin in toml["plugins"]:
        name = Path(plugin["path"]).name
        target = context.serialize_dir if name in entrypoint["artifact_plugins"] else context.plugins_dir
        shutil.copytree(REPO_ROOT / plugin["path"], target / name,
                        ignore=shutil.ignore_patterns("__pycache__"))

    jobs = _RecordingJobs()
    client = type("Client", (), {"jobs": jobs})()
    monkeypatch.setattr(auth_client, "get_authenticated_worker_client", lambda log, kind: client)

    log = structlog.get_logger()
    config = _job_config(toml, entrypoint)
    plugins = _artifact_plugins(toml, entrypoint)
    job_runner._create_engine_schema(context=context, plugins=plugins, log=log)
    artifact_tasks = job_runner._build_artifact_tasks(context=context, plugins=plugins, log=log)
    return {
        "config": config,
        "context": context,
        "artifact_tasks": artifact_tasks,
        "jobs": jobs,
        "log": log,
        "cwd": tmp_path,
    }


def test_job_config_passes_dioptras_validation(engine_run):
    issues = list(compile_swaps_config(engine_run["config"]).validate())
    assert not issues, [issue.message for issue in issues]
    # Same check the job runner makes, with the artifact-task schema it generated.
    job_runner._validate_yaml(engine_run["config"], engine_run["log"])


def test_artifact_task_is_discovered(engine_run):
    assert list(engine_run["artifact_tasks"]) == ["AtrHitsTableArtifactTask"]


def test_graph_runs_and_records_artifacts_and_metrics(engine_run):
    context = engine_run["context"]
    with env_vars({"__JOB_ID": "42"}):
        run_experiment(
            experiment_desc=copy.deepcopy(engine_run["config"]),
            global_parameters={
                "manifest_location": str(SAMPLES),
                "rules_dir": "",
                "s3_download_dir": "",
            },
            artifact_parameters={},
            artifact_tasks=engine_run["artifact_tasks"],
            artifacts_dir=context.artifacts_dir,
            plugins_dir=context.plugins_dir,
            serialize_dir=context.serialize_dir,
            deserialize_dir=context.deserialize_dir,
        )

    metrics = {c["metric_name"]: c["metric_value"] for c in engine_run["jobs"].calls}
    assert {c["job_id"] for c in engine_run["jobs"].calls} == {"42"}
    assert metrics["files_scanned"] == 2.0
    assert metrics["files_with_findings"] == 1.0
    assert metrics["total_hits"] > 0

    outputs = json.loads((context.dioptra_dir / "artifacts.json").read_text())["artifacts"]
    assert sorted(Path(a["path"]).name for a in outputs) == ["atr_hits.json", "atr_hits_csv.csv"]
    rows = json.loads((engine_run["cwd"] / "atr_hits.json").read_text())
    assert len(rows) == metrics["total_hits"]
    assert not any("benign" in row["file"] for row in rows)
