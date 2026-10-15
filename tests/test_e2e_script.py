"""Unit tests for scripts/dioptra_e2e.py (no Dioptra deployment needed)."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("requests", reason="requests not installed (pip install '.[e2e]')")

from atr_scanner import tasks  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dioptra_e2e.py"
_spec = importlib.util.spec_from_file_location("dioptra_e2e", SCRIPT)
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "expect_min_hits": 1,
        "expect_files_scanned": 2,
        "expect_files_with_findings": 1,
    }
    return argparse.Namespace(**{**defaults, **overrides})


def _results(**overrides):
    metrics = {name: 0.0 for name in e2e.EXPECTED_METRICS}
    metrics.update(total_hits=20.0, files_scanned=2.0, files_with_findings=1.0)
    base = {
        "metrics": metrics,
        "artifact_files": sorted(e2e.EXPECTED_ARTIFACT_FILES),
        "hits_rows": 20,
    }
    return {**base, **overrides}


def test_expected_metrics_match_what_the_plugin_records():
    assert tuple(e2e.EXPECTED_METRICS) == tasks.SCALAR_METRIC_NAMES


def test_all_checks_pass_for_a_complete_job():
    checks = e2e._checks(_args(), "finished", _results())
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


@pytest.mark.parametrize(
    ("status", "results", "failing"),
    [
        ("failed", _results(), "job finished"),
        ("finished", _results(artifact_files=["atr_hits.json"]), "artifacts recorded"),
        ("finished", _results(hits_rows=None), "hits artifact matches total_hits"),
        ("finished", _results(hits_rows=19), "hits artifact matches total_hits"),
        ("finished", _results(metrics={"total_hits": 20.0}), "all scalar metrics recorded"),
    ],
)
def test_each_missing_result_fails_its_check(status, results, failing):
    checks = {c["check"]: c["ok"] for c in e2e._checks(_args(), status, results)}
    assert checks[failing] is False


def test_file_expectations_are_checked_only_when_given():
    names = {c["check"] for c in e2e._checks(_args(expect_files_scanned=None), "finished", _results())}
    assert not any(name.startswith("files_scanned") for name in names)


def test_as_rows_accepts_bytes_or_parsed_json():
    assert e2e._as_rows(b'[{"rule_id": "R"}]') == [{"rule_id": "R"}]
    assert e2e._as_rows([{"rule_id": "R"}]) == [{"rule_id": "R"}]
    with pytest.raises(e2e.E2EError):
        e2e._as_rows({"rows": []})


def test_job_id_of_accepts_object_or_plain_id():
    assert e2e._job_id_of({"job": {"id": 5}}) == 5
    assert e2e._job_id_of({"job": 5}) == 5
    assert e2e._job_id_of({}) is None


def test_missing_credentials_are_a_run_error(monkeypatch, capsys):
    monkeypatch.delenv("DIOPTRA_USERNAME", raising=False)
    monkeypatch.delenv("DIOPTRA_PASSWORD", raising=False)
    assert e2e.main(["--archive", "x.tar.gz"]) == e2e.EXIT_RUN_ERROR
    assert "DIOPTRA_USERNAME" in capsys.readouterr().out
