"""Unit tests for the Dioptra task wrappers.

These patch ``build_pyatr_scan_fn`` so no real engine is needed; they verify
input resolution/validation, artifact shape, metrics, and error handling.
"""

from __future__ import annotations

import csv
import json
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


# ---- scan_manifests --------------------------------------------------------

def test_scan_manifests_returns_rows(patched_engine, tmp_path):
    (tmp_path / "bad.md").write_text("has BADWORD", encoding="utf-8")
    (tmp_path / "ok.md").write_text("all good", encoding="utf-8")
    rows = tasks.scan_manifests(str(tmp_path))
    assert any(r["rule_id"] == "FAKE-001" and r["file"] == "bad.md" for r in rows)
    assert all(isinstance(r["severity"], str) for r in rows)


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
