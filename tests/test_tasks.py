"""Unit tests for the Dioptra task wrappers.

These patch ``build_pyatr_scan_fn`` so no real engine is needed; they verify
input resolution/validation, artifact shape, metrics, and error handling.
"""

from __future__ import annotations

import csv
import json
import sys
import types
from pathlib import Path

import pytest

from atr_scanner import tasks


@pytest.fixture
def patched_engine(monkeypatch, fake_scan_fn):
    """Make scan_manifests use the fake engine instead of pyatr."""
    monkeypatch.setattr(tasks, "build_pyatr_scan_fn", lambda rules_dir=None: fake_scan_fn)
    return fake_scan_fn


# ---- resolve_input ---------------------------------------------------------

def test_resolve_input_local_dir(tmp_path):
    assert tasks.resolve_input(str(tmp_path)) == str(tmp_path)


def test_resolve_input_empty_raises():
    with pytest.raises(ValueError):
        tasks.resolve_input("   ")


def test_resolve_input_missing_local_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        tasks.resolve_input(str(tmp_path / "does-not-exist"))


def test_resolve_input_s3_requires_download_dir():
    with pytest.raises(ValueError):
        tasks.resolve_input("s3://bucket/prefix", s3_download_dir=None)


def test_resolve_input_s3_blank_download_dir_counts_as_missing():
    # Dioptra passes an unset optional string parameter as "" (its default).
    with pytest.raises(ValueError):
        tasks.resolve_input("s3://bucket/prefix", s3_download_dir="  ")


# ---- scan_manifests --------------------------------------------------------

def test_scan_manifests_returns_rows(patched_engine, tmp_path):
    (tmp_path / "bad.md").write_text("has BADWORD", encoding="utf-8")
    (tmp_path / "ok.md").write_text("all good", encoding="utf-8")
    rows = tasks.scan_manifests(str(tmp_path))
    assert any(r["rule_id"] == "FAKE-001" and r["file"] == "bad.md" for r in rows)
    assert all(isinstance(r["severity"], str) for r in rows)


@pytest.mark.parametrize("rules_dir", ["", "   ", None])
def test_scan_manifests_blank_rules_dir_uses_bundled_rules(
    monkeypatch, fake_scan_fn, tmp_path, rules_dir
):
    # The entrypoint default for rules_dir is "", which must mean "bundled
    # rules", not "load rules from the current directory".
    seen = []

    def fake_builder(rules_dir=None):
        seen.append(rules_dir)
        return fake_scan_fn

    monkeypatch.setattr(tasks, "build_pyatr_scan_fn", fake_builder)
    (tmp_path / "ok.md").write_text("all good", encoding="utf-8")
    tasks.scan_manifests(str(tmp_path), rules_dir=rules_dir)
    assert seen == [None]


def test_scan_manifests_without_manifests_is_an_error(patched_engine, tmp_path):
    # Nothing to scan must not look like "scanned, no findings".
    (tmp_path / "image.png").write_bytes(b"not a manifest")
    (tmp_path / "SKILL.mdx").write_text("unsupported suffix", encoding="utf-8")
    with pytest.raises(ValueError, match="no manifest files"):
        tasks.scan_manifests(str(tmp_path))


def test_scan_manifests_passes_explicit_rules_dir(monkeypatch, fake_scan_fn, tmp_path):
    seen = []

    def fake_builder(rules_dir=None):
        seen.append(rules_dir)
        return fake_scan_fn

    monkeypatch.setattr(tasks, "build_pyatr_scan_fn", fake_builder)
    (tmp_path / "ok.md").write_text("all good", encoding="utf-8")
    tasks.scan_manifests(str(tmp_path), rules_dir=" /rules ")
    assert seen == ["/rules"]


# ---- write_hits_artifact ---------------------------------------------------

def test_write_hits_artifact_json_and_csv(tmp_path):
    hits = [
        {
            "rule_id": "R1",
            "severity": "critical",
            "category": "prompt-injection",
            "file": "a.md",
            "title": "T",
            "confidence": "high",
            "event_types": "llm_input",
            "matched_patterns": "x",
        }
    ]
    out = tmp_path / "out"
    json_path = tasks.write_hits_artifact(hits, str(out))

    assert Path(json_path).name == "atr_hits.json"
    assert json.loads(Path(json_path).read_text()) == hits

    csv_path = out / "atr_hits.csv"
    assert csv_path.exists()
    rows = list(csv.DictReader(csv_path.open()))
    assert rows[0]["rule_id"] == "R1"


def test_write_hits_artifact_csv_neutralises_formulas(tmp_path):
    hits = [{"rule_id": "R1", "file": "=HYPERLINK(1).md", "title": "+cmd", "severity": "low"}]
    out = tmp_path / "out"
    tasks.write_hits_artifact(hits, str(out))
    row = next(csv.DictReader((out / "atr_hits.csv").open()))
    assert row["file"] == "'=HYPERLINK(1).md"
    assert row["title"] == "'+cmd"
    # The JSON form keeps the exact values.
    assert json.loads((out / "atr_hits.json").read_text())[0]["file"] == "=HYPERLINK(1).md"


def test_write_hits_artifact_empty(tmp_path):
    json_path = tasks.write_hits_artifact([], str(tmp_path / "out"))
    assert json.loads(Path(json_path).read_text()) == []


# ---- summarize_hits --------------------------------------------------------

def test_summarize_hits_counts():
    hits = [
        {"severity": "critical", "category": "prompt-injection", "file": "a.md"},
        {"severity": "high", "category": "prompt-injection", "file": "a.md"},
        {"severity": "critical", "category": "context-exfiltration", "file": "b.md"},
    ]
    m = tasks.summarize_hits(hits)
    assert m["total_hits"] == 3
    assert m["critical"] == 2
    assert m["high"] == 1
    assert m["files_with_findings"] == 2
    assert m["category_breakdown"]["prompt-injection"] == 2


def test_summarize_hits_empty():
    m = tasks.summarize_hits([])
    assert m["total_hits"] == 0
    assert m["files_with_findings"] == 0
    assert m["critical"] == 0
    assert "files_scanned" not in m  # unknown without local_root


def test_summarize_hits_counts_files_scanned(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"not a manifest")
    hits = [{"severity": "low", "category": "c", "file": "a.md"}]

    m = tasks.summarize_hits(hits, local_root=str(tmp_path))
    assert m["files_scanned"] == 2
    assert m["files_with_findings"] == 1


def test_summarize_hits_blank_local_root_is_ignored():
    m = tasks.summarize_hits([], local_root="")
    assert "files_scanned" not in m


# ---- log_scan_metrics ------------------------------------------------------

_METRICS = {
    "total_hits": 3,
    "files_scanned": 2,
    "files_with_findings": 1,
    "critical": 2,
    "high": 1,
    "medium": 0,
    "low": 0,
    "severity_breakdown": {"critical": 2, "high": 1},
    "category_breakdown": {"prompt-injection": 3},
}


class _FakeJobsClient:
    def __init__(self, fail: bool = False, fail_first: int = 0):
        self.calls = []
        self.attempts = 0
        self._fail = fail
        self._fail_first = fail_first

    def append_metric_by_id(self, job_id, metric_name, metric_value):
        self.attempts += 1
        if self._fail or self.attempts <= self._fail_first:
            raise RuntimeError("REST API unavailable")
        self.calls.append((job_id, metric_name, metric_value))


def _install_fake_worker_client(monkeypatch, jobs_client):
    """Stand in for dioptra.sdk.utilities.auth_client (dioptra not installed)."""
    client = types.SimpleNamespace(jobs=jobs_client)
    auth_client = types.ModuleType("dioptra.sdk.utilities.auth_client")
    auth_client.get_authenticated_worker_client = lambda log, client_type: client
    for name in ("dioptra", "dioptra.sdk", "dioptra.sdk.utilities"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "dioptra.sdk.utilities.auth_client", auth_client)


def test_log_scan_metrics_outside_a_dioptra_job_needs_no_client(monkeypatch):
    monkeypatch.delenv(tasks.DIOPTRA_JOB_ID_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "dioptra.sdk.utilities.auth_client", None)
    assert tasks.log_scan_metrics(_METRICS) is None


def test_log_scan_metrics_records_scalars_on_the_job(monkeypatch):
    jobs = _FakeJobsClient()
    _install_fake_worker_client(monkeypatch, jobs)
    monkeypatch.setenv(tasks.DIOPTRA_JOB_ID_ENV, "42")

    tasks.log_scan_metrics(_METRICS)

    recorded = {name: value for _, name, value in jobs.calls}
    assert {job_id for job_id, _, _ in jobs.calls} == {"42"}
    assert recorded == {name: float(_METRICS[name]) for name in tasks.SCALAR_METRIC_NAMES}
    assert all(isinstance(value, float) for value in recorded.values())


def test_log_scan_metrics_skips_absent_scalars(monkeypatch):
    jobs = _FakeJobsClient()
    _install_fake_worker_client(monkeypatch, jobs)
    monkeypatch.setenv(tasks.DIOPTRA_JOB_ID_ENV, "7")
    without_files_scanned = {k: v for k, v in _METRICS.items() if k != "files_scanned"}

    tasks.log_scan_metrics(without_files_scanned)

    assert "files_scanned" not in {name for _, name, _ in jobs.calls}


def test_log_scan_metrics_raises_when_recording_keeps_failing(monkeypatch):
    jobs = _FakeJobsClient(fail=True)
    _install_fake_worker_client(monkeypatch, jobs)
    monkeypatch.setattr(tasks.time, "sleep", lambda seconds: None)
    monkeypatch.setenv(tasks.DIOPTRA_JOB_ID_ENV, "42")
    with pytest.raises(RuntimeError):
        tasks.log_scan_metrics(_METRICS)
    assert jobs.attempts == tasks.METRIC_RECORD_ATTEMPTS


def test_log_scan_metrics_retries_a_transient_failure(monkeypatch):
    jobs = _FakeJobsClient(fail_first=1)
    _install_fake_worker_client(monkeypatch, jobs)
    monkeypatch.setattr(tasks.time, "sleep", lambda seconds: None)
    monkeypatch.setenv(tasks.DIOPTRA_JOB_ID_ENV, "42")

    tasks.log_scan_metrics(_METRICS)

    assert {name for _, name, _ in jobs.calls} == set(tasks.SCALAR_METRIC_NAMES)


@pytest.mark.parametrize("bad", [{}, {"files_scanned": 2}, [], None])
def test_log_scan_metrics_rejects_input_that_is_not_a_summary(monkeypatch, bad):
    monkeypatch.delenv(tasks.DIOPTRA_JOB_ID_ENV, raising=False)
    with pytest.raises(ValueError):
        tasks.log_scan_metrics(bad)


# ---- atr_scan_and_report (chained) -----------------------------------------

def test_atr_scan_and_report_end_to_end(patched_engine, tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "bad.md").write_text("BADWORD and EXFIL", encoding="utf-8")
    (manifests / "ok.md").write_text("innocuous", encoding="utf-8")

    result = tasks.atr_scan_and_report(
        manifest_location=str(manifests),
        output_dir=str(tmp_path / "out"),
    )
    assert Path(result["artifact_path"]).exists()
    assert result["metrics"]["total_hits"] >= 1
    assert result["metrics"]["critical"] >= 1
    assert result["metrics"]["files_with_findings"] == 1  # only bad.md hit
    assert result["metrics"]["files_scanned"] == 2


# ---- S3 download -----------------------------------------------------------

class _FakeS3:
    def __init__(self, keys):
        self._keys = keys
        self.downloaded = []

    def get_paginator(self, name):
        keys = self._keys
        return types.SimpleNamespace(
            paginate=lambda Bucket, Prefix: [{"Contents": [{"Key": k} for k in keys]}]
        )

    def download_file(self, bucket, key, target):
        Path(target).write_text(key, encoding="utf-8")
        self.downloaded.append(Path(target))


def _install_fake_boto3(monkeypatch, s3):
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda service: s3
    monkeypatch.setitem(sys.modules, "boto3", boto3)


def test_s3_download_keeps_objects_under_the_download_dir(monkeypatch, tmp_path):
    s3 = _FakeS3(["skills/a.md", "skills/nested/b.json", "skills/dir/"])
    _install_fake_boto3(monkeypatch, s3)
    dest = tmp_path / "dl"

    local = tasks.resolve_input("s3://bucket/skills", s3_download_dir=str(dest))

    assert local == str(dest)
    assert sorted(p.relative_to(dest.resolve()).as_posix() for p in s3.downloaded) == [
        "a.md",
        "nested/b.json",
    ]


def test_s3_uri_naming_a_single_object(monkeypatch, tmp_path):
    s3 = _FakeS3(["skills/a.md"])
    _install_fake_boto3(monkeypatch, s3)
    dest = tmp_path / "dl"
    tasks.resolve_input("s3://bucket/skills/a.md", s3_download_dir=str(dest))
    assert s3.downloaded == [dest.resolve() / "a.md"]


@pytest.mark.parametrize("key", ["skills/../../escape.md", "skills/../escape.md", "skills/.."])
def test_s3_download_rejects_keys_outside_the_download_dir(monkeypatch, tmp_path, key):
    s3 = _FakeS3([key])
    _install_fake_boto3(monkeypatch, s3)
    with pytest.raises(ValueError, match="outside s3_download_dir"):
        tasks.resolve_input("s3://bucket/skills", s3_download_dir=str(tmp_path / "dl"))
    assert s3.downloaded == []
