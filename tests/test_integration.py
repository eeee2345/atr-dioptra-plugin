"""Integration smoke test using the REAL pyatr engine.

Skipped automatically if pyatr is not installed, so the unit suite still runs
green in a minimal environment. When pyatr is present this proves the wrapper's
end-to-end contract against the actual ATR Layer-1 rules and the real sample
manifests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyatr", reason="pyatr not installed; skipping integration smoke")

from atr_scanner import tasks  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "examples" / "sample_manifests"


@pytest.mark.integration
def test_real_scan_flags_malicious_and_clears_benign(tmp_path):
    result = tasks.atr_scan_and_report(
        manifest_location=str(SAMPLES),
        output_dir=str(tmp_path / "out"),
    )
    hits = result["hits"]
    metrics = result["metrics"]

    # The malicious sample must produce hits, at least one critical.
    assert metrics["total_hits"] > 0
    assert metrics["critical"] > 0

    hit_files = {h["file"] for h in hits}
    assert any("malicious" in f for f in hit_files), (
        "expected the malicious sample manifest to be flagged"
    )
    # The benign sample must produce zero hits.
    assert not any("benign" in f for f in hit_files), (
        "benign sample manifest should not produce hits"
    )

    # Every hit row must carry a real category (from tags['category']).
    assert all(h["category"] and h["category"] != "uncategorized" for h in hits)


@pytest.mark.integration
def test_real_scan_single_benign_file_is_clean(tmp_path):
    benign = SAMPLES / "benign-unit-converter.md"
    result = tasks.atr_scan_and_report(
        manifest_location=str(benign),
        output_dir=str(tmp_path / "out"),
    )
    assert result["metrics"]["total_hits"] == 0
