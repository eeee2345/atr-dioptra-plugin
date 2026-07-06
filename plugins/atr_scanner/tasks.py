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
    event_types, matched_patterns}.

write_hits_artifact(hits, output_dir) -> artifact_path: str
    Writes ``hits`` as both ``atr_hits.json`` and ``atr_hits.csv`` under
    ``output_dir``. Returns the JSON path.

summarize_hits(hits) -> metrics: dict
    Scalar metrics for Dioptra: total_hits, plus per-severity and per-category
    counts, and files_with_findings.

atr_scan_and_report(...) -> dict
    Convenience one-shot that chains the four steps above (used by the CLI and
    integration smoke test).
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .scanner import (
    DEFAULT_EVENT_TYPES,
    Scanner,
    build_pyatr_scan_fn,
    discover_manifest_files,
)

# Task name constants — the single source of truth. The always-on
# reconciliation test asserts that dioptra.toml declares exactly these names.
TASK_RESOLVE_INPUT = "resolve_input"
TASK_SCAN_MANIFESTS = "scan_manifests"
TASK_WRITE_HITS_ARTIFACT = "write_hits_artifact"
TASK_SUMMARIZE_HITS = "summarize_hits"
TASK_ATR_SCAN_AND_REPORT = "atr_scan_and_report"

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
        return _download_s3_prefix(location, s3_download_dir)

    local = Path(location).expanduser()
    if not local.exists():
        raise FileNotFoundError(f"manifest_location does not exist: {local}")
    return str(local)


def scan_manifests(
    local_root: str,
    rules_dir: str | None = None,
) -> list[dict[str, str]]:
    """Scan every manifest under ``local_root`` with the real pyatr engine."""
    root = Path(local_root).expanduser()
    files = discover_manifest_files(root)
    scanner = Scanner(
        scan_fn=build_pyatr_scan_fn(rules_dir),
        event_types=DEFAULT_EVENT_TYPES,
    )
    scan_root = root if root.is_dir() else root.parent
    hits = scanner.scan_paths(files, root=scan_root)
    return [hit.to_row() for hit in hits]


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
            writer.writerow({col: row.get(col, "") for col in _CSV_COLUMNS})

    return str(json_path)


def summarize_hits(hits: list[dict[str, str]]) -> dict[str, Any]:
    """Compute scalar metrics from the hits table for Dioptra to record."""
    severity_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    files_with_findings: set[str] = set()

    for row in hits:
        severity_counts[row.get("severity", "") or "unknown"] += 1
        category_counts[row.get("category", "") or "uncategorized"] += 1
        file_label = row.get("file")
        if file_label:
            files_with_findings.add(file_label)

    return {
        "total_hits": len(hits),
        "files_with_findings": len(files_with_findings),
        "critical": severity_counts.get("critical", 0),
        "high": severity_counts.get("high", 0),
        "medium": severity_counts.get("medium", 0),
        "low": severity_counts.get("low", 0),
        "severity_breakdown": dict(severity_counts),
        "category_breakdown": dict(category_counts),
    }


def atr_scan_and_report(
    manifest_location: str,
    output_dir: str,
    rules_dir: str | None = None,
    s3_download_dir: str | None = None,
) -> dict[str, Any]:
    """One-shot: resolve -> scan -> write artifact -> summarize.

    Returns a dict with ``hits``, ``artifact_path`` and ``metrics``. Used by the
    CLI and the integration smoke test; the Dioptra task-graph calls the four
    granular tasks individually so each output is a first-class Dioptra value.
    """
    local_root = resolve_input(manifest_location, s3_download_dir)
    hits = scan_manifests(local_root, rules_dir)
    artifact_path = write_hits_artifact(hits, output_dir)
    metrics = summarize_hits(hits)
    return {
        "hits": hits,
        "artifact_path": artifact_path,
        "metrics": metrics,
    }


def _download_s3_prefix(uri: str, s3_download_dir: str | None) -> str:
    """Download all objects under an s3:// prefix to a local dir.

    Kept import-lazy so the plugin works fully offline against local manifests
    without boto3 installed. Only triggered when the input is an s3:// URI.
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
            rel = key[len(prefix):].lstrip("/") if prefix else key
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            downloaded += 1

    if downloaded == 0:
        raise FileNotFoundError(f"no objects found under s3 URI: {uri}")
    return str(dest)
