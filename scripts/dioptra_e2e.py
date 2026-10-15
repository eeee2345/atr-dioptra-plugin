#!/usr/bin/env python3
"""End-to-end smoke test of the ATR plugin against a running Dioptra deployment.

Drives the Dioptra REST API the way a user would:

  1. log in (optionally registering the account first),
  2. import this repository's resources (dioptra.toml) from a git URL or from a
     local .tar.gz archive,
  3. make sure a queue served by the ATR worker exists and is attached to the
     "ATR Manifest Scan" entrypoint,
  4. create an experiment and submit a job,
  5. wait for the job, then check that it finished and that the expected
     metrics and artifacts were recorded on it.

Prints a JSON report on stdout. Exit status: 0 all checks passed, 1 a check
failed, 2 the run itself could not complete. Credentials are read from the
environment only (DIOPTRA_USERNAME, DIOPTRA_PASSWORD), never from the command
line.

Example:
    DIOPTRA_USERNAME=... DIOPTRA_PASSWORD=... python scripts/dioptra_e2e.py \\
        --archive build/atr-dioptra-plugin.tar.gz --queue atr-scanner \\
        --manifest-location /dioptra/data/atr-manifests --expect-files-scanned 2
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import requests

ENTRYPOINT_NAME = "ATR Manifest Scan"
EXPECTED_ARTIFACT_FILES = ("atr_hits.json", "atr_hits_csv.csv")
# Must equal atr_scanner.tasks.SCALAR_METRIC_NAMES (a test reconciles them).
EXPECTED_METRICS = (
    "total_hits",
    "files_scanned",
    "files_with_findings",
    "critical",
    "high",
    "medium",
    "low",
)
TERMINAL_JOB_STATES = {"finished", "failed"}
EXIT_OK, EXIT_CHECKS_FAILED, EXIT_RUN_ERROR = 0, 1, 2


class E2EError(RuntimeError):
    """A step of the end-to-end run failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("DIOPTRA_URL", "http://localhost"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--git-url", help="public GitHub/GitLab URL, optionally #ref")
    source.add_argument("--archive", type=Path, help=".tar.gz of the repository root")
    parser.add_argument("--config-path", default="dioptra.toml")
    parser.add_argument("--group", type=int, default=None, help="group id (default: first)")
    parser.add_argument("--queue", default="atr-scanner")
    parser.add_argument("--manifest-location", default="/dioptra/data/atr-manifests")
    parser.add_argument("--register", action="store_true", help="create the user if needed")
    parser.add_argument("--email", default="atr-e2e@example.com")
    parser.add_argument("--timeout", type=float, default=600.0, help="job wait, seconds")
    parser.add_argument("--expect-files-scanned", type=int, default=None)
    parser.add_argument("--expect-files-with-findings", type=int, default=None)
    parser.add_argument("--expect-min-hits", type=int, default=1)
    return parser.parse_args(argv)


class DioptraApi:
    """Minimal REST client: one session, JSON in/out, explicit errors."""

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/") + "/api/v1"
        self._session = requests.Session()

    def request(self, method: str, path: str, **kwargs) -> Any:
        response = self._session.request(method, f"{self._base}{path}", timeout=120, **kwargs)
        if response.status_code >= 400:
            raise E2EError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:2000]}",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        if "application/json" in response.headers.get("Content-Type", ""):
            return response.json()
        return response.content

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)

    def list_all(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            page = self.get(path, params={"index": len(items), "pageLength": 100})
            data = page.get("data", [])
            items.extend(data)
            if page.get("isComplete", True) or not data:
                return items


def _login(api: DioptraApi, username: str, password: str, register: bool, email: str) -> None:
    credentials = {"username": username, "password": password}
    try:
        api.post("/auth/login", json=credentials)
        return
    except E2EError as login_error:
        # Only a rejected login may mean "no such account yet".
        if not register or login_error.status_code != 401:
            raise
        try:
            api.post(
                "/users/",
                json={**credentials, "email": email, "confirmPassword": password},
            )
        except E2EError as register_error:
            raise E2EError(
                f"login failed ({login_error}) and registration failed ({register_error})"
            ) from register_error
    api.post("/auth/login", json=credentials)


def _import_resources(api: DioptraApi, args: argparse.Namespace, group: int) -> dict[str, Any]:
    data = {
        "group": str(group),
        "configPath": args.config_path,
        "resolveNameConflictsStrategy": "overwrite",
    }
    if args.git_url:
        return api.post(
            "/workflows/resourceImport",
            data={**data, "sourceType": "git", "gitUrl": args.git_url},
        )
    return api.post(
        "/workflows/resourceImport",
        data={**data, "sourceType": "upload_archive"},
        files={"archiveFile": (args.archive.name, args.archive.read_bytes(), "application/gzip")},
    )


def _ensure_queue(api: DioptraApi, name: str, group: int) -> int:
    existing = [q["id"] for q in api.list_all("/queues/") if q["name"] == name]
    if existing:
        return existing[0]
    created = api.post(
        "/queues/",
        json={"group": group, "name": name, "description": "Served by the ATR worker."},
    )
    return created["id"]


def _submit_job(api: DioptraApi, args: argparse.Namespace, group: int, entrypoint_id: int,
                queue_id: int) -> tuple[int, int]:
    api.post(f"/entrypoints/{entrypoint_id}/queues", json={"ids": [queue_id]})
    experiment = api.post(
        "/experiments/",
        json={
            "group": group,
            "name": f"atr-e2e-{int(time.time())}",
            "description": "ATR plugin end-to-end smoke test",
            "entrypoints": [entrypoint_id],
        },
    )
    job = api.post(
        f"/experiments/{experiment['id']}/jobs",
        json={
            "queue": queue_id,
            "entrypoint": entrypoint_id,
            "values": {"manifest_location": args.manifest_location},
            "description": "ATR manifest scan (e2e)",
        },
    )
    return experiment["id"], job["id"]


def _wait_for_job(api: DioptraApi, job_id: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while True:
        status = api.get(f"/jobs/{job_id}")["status"]
        if status in TERMINAL_JOB_STATES:
            return status
        if time.monotonic() > deadline:
            raise E2EError(f"job {job_id} still '{status}' after {timeout}s")
        time.sleep(3)


def _job_id_of(artifact: dict[str, Any]) -> Any:
    job = artifact.get("job")
    return job.get("id") if isinstance(job, dict) else job


def _as_rows(contents: Any) -> list[dict[str, Any]]:
    """The contents endpoint may return raw bytes or already-parsed JSON."""
    rows = json.loads(contents) if isinstance(contents, (bytes, str)) else contents
    if not isinstance(rows, list):
        raise E2EError(f"hits artifact is not a list of rows: {type(rows).__name__}")
    return rows


def _collect_results(api: DioptraApi, job_id: int) -> dict[str, Any]:
    metrics = {m["name"]: m["value"] for m in api.get(f"/jobs/{job_id}/metrics")}
    artifacts = [a for a in api.list_all("/artifacts/") if _job_id_of(a) == job_id]
    by_file = {Path(a["artifactUri"]).name: a for a in artifacts}
    hits_json = by_file.get("atr_hits.json")
    rows = _as_rows(api.get(f"/artifacts/{hits_json['id']}/contents")) if hits_json else None
    return {
        "metrics": metrics,
        "artifacts": [
            {"id": a["id"], "uri": a["artifactUri"], "file_size": a.get("fileSize")}
            for a in artifacts
        ],
        "artifact_files": sorted(by_file),
        "hits_rows": None if rows is None else len(rows),
    }


def _job_log(api: DioptraApi, job_id: int) -> list[Any]:
    try:
        return [f"{r.get('loggerName')}: {r.get('message')}" for r in api.list_all(f"/jobs/{job_id}/log")]
    except (E2EError, requests.RequestException) as exc:
        return [f"could not fetch the job log: {exc}"]


def _checks(args: argparse.Namespace, status: str, results: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = results["metrics"]

    def check(name: str, ok: bool, detail: Any) -> dict[str, Any]:
        return {"check": name, "ok": bool(ok), "detail": detail}

    checks = [
        check("job finished", status == "finished", status),
        check("all scalar metrics recorded", set(EXPECTED_METRICS) <= set(metrics),
              sorted(set(EXPECTED_METRICS) - set(metrics))),
        check(f"total_hits >= {args.expect_min_hits}",
              metrics.get("total_hits", 0) >= args.expect_min_hits, metrics.get("total_hits")),
        check("artifacts recorded", set(EXPECTED_ARTIFACT_FILES) <= set(results["artifact_files"]),
              results["artifact_files"]),
        check("hits artifact matches total_hits",
              results["hits_rows"] is not None and results["hits_rows"] == metrics.get("total_hits"),
              {"rows": results["hits_rows"], "total_hits": metrics.get("total_hits")}),
    ]
    expectations = (
        ("files_scanned", args.expect_files_scanned),
        ("files_with_findings", args.expect_files_with_findings),
    )
    return checks + [
        check(f"{name} == {expected}", metrics.get(name) == expected, metrics.get(name))
        for name, expected in expectations
        if expected is not None
    ]


def run(args: argparse.Namespace) -> dict[str, Any]:
    username = os.environ.get("DIOPTRA_USERNAME")
    password = os.environ.get("DIOPTRA_PASSWORD")
    if not username or not password:
        raise E2EError("set DIOPTRA_USERNAME and DIOPTRA_PASSWORD in the environment")

    api = DioptraApi(args.url)
    _login(api, username, password, args.register, args.email)
    group = args.group or api.list_all("/groups/")[0]["id"]
    imported = _import_resources(api, args, group)
    entrypoint_id = imported["resources"]["entrypoints"][ENTRYPOINT_NAME]
    queue_id = _ensure_queue(api, args.queue, group)
    experiment_id, job_id = _submit_job(api, args, group, entrypoint_id, queue_id)
    status = _wait_for_job(api, job_id, args.timeout)
    results = _collect_results(api, job_id)
    return {
        "dioptra_url": args.url,
        "import": imported,
        "queue_id": queue_id,
        "experiment_id": experiment_id,
        "job_id": job_id,
        "job_status": status,
        **results,
        "checks": _checks(args, status, results),
        **({} if status == "finished" else {"job_log": _job_log(api, job_id)}),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = run(args)
    except Exception as exc:  # noqa: BLE001 (CLI boundary: report, do not trace)
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return EXIT_RUN_ERROR
    print(json.dumps(report, indent=2, default=str))
    return EXIT_OK if all(c["ok"] for c in report["checks"]) else EXIT_CHECKS_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
