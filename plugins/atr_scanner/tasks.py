"""Dioptra task entrypoints for the ATR scanner plugin.

Each function here is registered as a Dioptra task in ``dioptra.toml`` under
``[[plugins.tasks.functions]]``. The task-graph in
``entrypoints/atr-scan.yaml`` wires them together.

The functions are deliberately thin: all real scanning logic lives in
:mod:`scanner` (which has zero Dioptra dependency and is fully unit-testable).
These wrappers only:

  1. resolve/validate the input location (local dir/file or S3 URI),
  2. drive the scanner,
  3. shape the output into Dioptra-friendly artifacts + metrics.

INPUT / OUTPUT CONTRACT
-----------------------
resolve_input(manifest_location, s3_download_dir) -> local_root: str
    Accepts a local path OR an ``s3://bucket/key`` URI. For S3 the objects are
    downloaded under ``s3_download_dir`` (requires boto3). Returns a local
    directory or file path.

scan_manifests(local_root, rules_dir) -> hits: list[dict]
    Runs the ATR engine over every manifest under ``local_root``. Returns a
    list of flat rows: {rule_id, severity, category, file, title, confidence,
    event_types, matched_patterns}. Finding no manifest to scan is an error.

write_hits_artifact(hits, output_dir) -> artifact_path: str
    Writes ``hits`` as both ``atr_hits.json`` and ``atr_hits.csv`` under
    ``output_dir``. Returns the JSON path. (The Dioptra entrypoint stores the
    table through the ``atr_artifacts`` plugin instead.)

summarize_hits(hits, local_root) -> metrics: dict
    Scalar metrics for Dioptra: total_hits, plus per-severity and per-category
    counts, files_with_findings and (when ``local_root`` is given)
    files_scanned.

log_scan_metrics(metrics) -> None
    Records the scalar metrics on the running Dioptra job through the worker's
    authenticated client. Outside a Dioptra job it only logs them.

atr_scan_and_report(...) -> dict
    Convenience one-shot that chains resolve -> scan -> write -> summarize
    (used by the CLI and integration smoke test).

DIOPTRA REGISTRATION
--------------------
The Dioptra task engine resolves graph steps through ``dioptra.pyplugs``, so
every task is decorated with ``pyplugs.register``. ``dioptra`` is only
importable inside a Dioptra worker; outside it (CLI, unit tests) a no-op
stand-in keeps the plugin usable without a Dioptra install.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
import types
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .scanner import (
    DEFAULT_EVENT_TYPES,
    MANIFEST_SUFFIXES,
    Scanner,
    build_pyatr_scan_fn,
    discover_manifest_files,
)

try:
    from dioptra import pyplugs
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
    if exc.name != "dioptra":
        raise
    # Outside a Dioptra worker registration is a no-op.
    pyplugs = types.SimpleNamespace(register=lambda func: func)


LOGGER = logging.getLogger(__name__)

# Task name constants — the single source of truth. The always-on
# reconciliation test asserts that dioptra.toml declares exactly these names.
TASK_RESOLVE_INPUT = "resolve_input"
TASK_SCAN_MANIFESTS = "scan_manifests"
TASK_WRITE_HITS_ARTIFACT = "write_hits_artifact"
TASK_SUMMARIZE_HITS = "summarize_hits"
TASK_LOG_SCAN_METRICS = "log_scan_metrics"
TASK_ATR_SCAN_AND_REPORT = "atr_scan_and_report"

# Environment variable the Dioptra job runner sets while a job's graph runs.
DIOPTRA_JOB_ID_ENV = "__JOB_ID"

# Scalar metrics recorded on the Dioptra job (breakdown dicts stay in logs and
# in the hits-table artifact; Dioptra metrics are name -> number).
SCALAR_METRIC_NAMES: tuple[str, ...] = (
    "total_hits",
    "files_scanned",
    "files_with_findings",
    "critical",
    "high",
    "medium",
    "low",
)
# Attempts per metric before a REST failure fails the job (transient errors).
METRIC_RECORD_ATTEMPTS = 3

# Plugin param type names — must match [[plugin_param_types]] in dioptra.toml.
PARAM_TYPE_HITS_TABLE = "atr_hits_table"
PARAM_TYPE_METRICS = "atr_metrics"

S3_PREFIX = "s3://"

_JSON_NAME = "atr_hits.json"
_CSV_NAME = "atr_hits.csv"
_CSV_COLUMNS = (
    "rule_id",
    "severity",
    "category",
    "file",
    "title",
    "confidence",
    "event_types",
    "matched_patterns",
)
# Leading characters a spreadsheet would evaluate as a formula. Manifest file
# names are untrusted input, so CSV cells starting with one are neutralised.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _optional(value: str | None) -> str | None:
    """Treat a blank optional parameter as "not provided".

    Dioptra entrypoint parameters are typed strings, so an optional parameter
    left at its default arrives as ``""`` rather than ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _csv_cell(value: Any) -> str:
    """Render a value for CSV without letting it become a spreadsheet formula."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_CSV_FORMULA_PREFIXES) else text


@pyplugs.register
def resolve_input(
    manifest_location: str,
    s3_download_dir: str | None = None,
) -> str:
    """Resolve a manifest location to a local path.

    Accepts either a local directory/file path or an ``s3://bucket/key`` URI.
    Validates at the boundary and fails fast with a clear message.
    """
    if not manifest_location or not str(manifest_location).strip():
        raise ValueError("manifest_location must be a non-empty string")

    location = str(manifest_location).strip()

    if location.startswith(S3_PREFIX):
        return _download_s3_prefix(location, _optional(s3_download_dir))

    local = Path(location).expanduser()
    if not local.exists():
        raise FileNotFoundError(f"manifest_location does not exist: {local}")
    return str(local)


@pyplugs.register
def scan_manifests(
    local_root: str,
    rules_dir: str | None = None,
) -> list[dict[str, str]]:
    """Scan every manifest under ``local_root`` with the real pyatr engine.

    A location without any manifest is an error, not a clean result: an empty
    scan would otherwise look identical to "no findings".
    """
    root = Path(local_root).expanduser()
    files = discover_manifest_files(root)
    if not files:
        raise ValueError(
            f"no manifest files ({' '.join(MANIFEST_SUFFIXES)}) found under {root}"
        )
    scanner = Scanner(
        scan_fn=build_pyatr_scan_fn(_optional(rules_dir)),
        event_types=DEFAULT_EVENT_TYPES,
    )
    scan_root = root if root.is_dir() else root.parent
    hits = scanner.scan_paths(files, root=scan_root)
    return [hit.to_row() for hit in hits]


@pyplugs.register
def write_hits_artifact(
    hits: list[dict[str, str]],
    output_dir: str,
) -> str:
    """Persist hits as JSON + CSV artifacts. Returns the JSON path."""
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    json_path = out / _JSON_NAME
    json_path.write_text(
        json.dumps(hits, indent=2, sort_keys=False),
        encoding="utf-8",
    )

    csv_path = out / _CSV_NAME
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in hits:
            writer.writerow({col: _csv_cell(row.get(col)) for col in _CSV_COLUMNS})

    return str(json_path)


@pyplugs.register
def summarize_hits(
    hits: list[dict[str, str]],
    local_root: str | None = None,
) -> dict[str, Any]:
    """Compute scalar metrics from the hits table for Dioptra to record.

    ``local_root`` (the scanned path) is optional; when given, the number of
    manifests scanned is reported too, so clean files are visible in the
    metrics and not only files that produced hits.
    """
    severity_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    files_with_findings: set[str] = set()

    for row in hits:
        severity_counts[row.get("severity", "") or "unknown"] += 1
        category_counts[row.get("category", "") or "uncategorized"] += 1
        file_label = row.get("file")
        if file_label:
            files_with_findings.add(file_label)

    metrics: dict[str, Any] = {
        "total_hits": len(hits),
        "files_with_findings": len(files_with_findings),
        "critical": severity_counts.get("critical", 0),
        "high": severity_counts.get("high", 0),
        "medium": severity_counts.get("medium", 0),
        "low": severity_counts.get("low", 0),
        "severity_breakdown": dict(severity_counts),
        "category_breakdown": dict(category_counts),
    }
    scanned_root = _optional(local_root)
    if scanned_root is None:
        return metrics
    files_scanned = len(discover_manifest_files(Path(scanned_root).expanduser()))
    return {**metrics, "files_scanned": files_scanned}


@pyplugs.register
def log_scan_metrics(metrics: dict[str, Any]) -> None:
    """Record the scalar scan metrics on the running Dioptra job.

    Inside a Dioptra job the runner exports ``__JOB_ID``; each scalar metric is
    then appended to that job through the worker's authenticated client (the
    pattern Dioptra's own plugins use). A failure to record, after a few
    attempts, is raised rather than swallowed: a scan whose metrics went
    missing must not show up as a successful job. Outside a Dioptra job the
    metrics are only logged.
    """
    if not isinstance(metrics, dict) or "total_hits" not in metrics:
        raise ValueError("log_scan_metrics expects the summarize_hits output")
    scalars = {
        name: float(metrics[name]) for name in SCALAR_METRIC_NAMES if name in metrics
    }
    job_id = os.environ.get(DIOPTRA_JOB_ID_ENV)
    if not job_id:
        LOGGER.info("Not inside a Dioptra job; scan metrics not recorded: %s", scalars)
        return None

    from dioptra.sdk.utilities.auth_client import (  # noqa: PLC0415 (worker-only)
        get_authenticated_worker_client,
    )

    client = get_authenticated_worker_client(LOGGER, "json")
    for name, value in scalars.items():
        _append_metric(client, job_id, name, value)
    LOGGER.info("Recorded %d scan metrics on Dioptra job %s", len(scalars), job_id)
    return None


def _append_metric(client: Any, job_id: str, name: str, value: float) -> None:
    """Append one metric, retrying transient failures a bounded number of times."""
    for attempt in range(1, METRIC_RECORD_ATTEMPTS + 1):
        try:
            client.jobs.append_metric_by_id(
                job_id=job_id,
                metric_name=name,
                metric_value=value,
            )
            return
        except Exception as exc:  # noqa: BLE001 (re-raised after the last attempt)
            if attempt == METRIC_RECORD_ATTEMPTS:
                raise
            LOGGER.warning(
                "Recording metric %s on job %s failed (attempt %d/%d): %s",
                name,
                job_id,
                attempt,
                METRIC_RECORD_ATTEMPTS,
                exc,
            )
            time.sleep(attempt)


@pyplugs.register
def atr_scan_and_report(
    manifest_location: str,
    output_dir: str,
    rules_dir: str | None = None,
    s3_download_dir: str | None = None,
) -> dict[str, Any]:
    """One-shot: resolve -> scan -> write artifact -> summarize.

    Returns a dict with ``hits``, ``artifact_path`` and ``metrics``. Used by the
    CLI and the integration smoke test; the Dioptra task-graph calls the
    granular tasks individually so each output is a first-class Dioptra value.
    """
    local_root = resolve_input(manifest_location, s3_download_dir)
    hits = scan_manifests(local_root, rules_dir)
    artifact_path = write_hits_artifact(hits, output_dir)
    metrics = summarize_hits(hits, local_root)
    return {
        "hits": hits,
        "artifact_path": artifact_path,
        "metrics": metrics,
    }


def _download_s3_prefix(uri: str, s3_download_dir: str | None) -> str:
    """Download all objects under an s3:// prefix to a local dir.

    Kept import-lazy so the plugin works fully offline against local manifests
    without boto3 installed. Only triggered when the input is an s3:// URI.
    The client uses the standard boto3 configuration (``AWS_*`` environment
    variables; ``AWS_ENDPOINT_URL_S3`` selects a non-AWS endpoint such as
    MinIO). Object keys that would land outside ``s3_download_dir`` (``..``
    segments, absolute keys) are rejected.
    """
    if not s3_download_dir:
        raise ValueError(
            "s3_download_dir is required when manifest_location is an s3:// URI"
        )

    try:
        import boto3  # noqa: PLC0415 (optional dependency, lazy)
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "boto3 is required to read s3:// manifest locations; "
            "install with pip install 'atr-dioptra-plugin[s3]'"
        ) from exc

    without_scheme = uri[len(S3_PREFIX):]
    bucket, _, prefix = without_scheme.partition("/")
    if not bucket:
        raise ValueError(f"invalid s3 URI (no bucket): {uri}")

    dest = Path(s3_download_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    client = boto3.client("s3")
    paginator = client.get_paginator("list_objects_v2")
    downloaded = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            target = _download_target(dest, key, prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            downloaded += 1

    if downloaded == 0:
        raise FileNotFoundError(f"no objects found under s3 URI: {uri}")
    return str(dest)


def _download_target(dest: Path, key: str, prefix: str) -> Path:
    """Local path for an object key, refusing anything outside ``dest``."""
    rel = key[len(prefix):].lstrip("/") if prefix else key.lstrip("/")
    if not rel:  # the URI named a single object: keep its file name
        rel = PurePosixPath(key).name
    root = dest.resolve()
    target = (root / rel).resolve()
    if root not in target.parents:
        raise ValueError(f"refusing s3 object key outside s3_download_dir: {key!r}")
    return target
