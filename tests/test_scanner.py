"""Unit tests for the pure scanner core (fake engine, no pyatr)."""

from __future__ import annotations

from pathlib import Path

import pytest

from atr_scanner.scanner import (
    DEFAULT_EVENT_TYPES,
    RuleHit,
    Scanner,
    discover_manifest_files,
)


def test_scan_text_dedups_rule_across_event_types(fake_scan_fn):
    scanner = Scanner(fake_scan_fn, event_types=DEFAULT_EVENT_TYPES)
    hits = scanner.scan_text("this has BADWORD in it", file_label="a.md")

    # FAKE-001 fires under both llm_input and tool_call -> exactly ONE row.
    fake001 = [h for h in hits if h.rule_id == "FAKE-001"]
    assert len(fake001) == 1
    assert set(fake001[0].event_types) == {"llm_input", "tool_call"}


def test_scan_text_extracts_category_from_tags(fake_scan_fn):
    scanner = Scanner(fake_scan_fn)
    hits = scanner.scan_text("BADWORD", file_label="a.md")
    hit = next(h for h in hits if h.rule_id == "FAKE-001")
    # Critical detail: category comes from tags['category'], not a top attr.
    assert hit.category == "prompt-injection"


def test_scan_text_uncategorized_fallback(fake_scan_fn):
    scanner = Scanner(fake_scan_fn)
    hits = scanner.scan_text("NOCAT here", file_label="a.md")
    hit = next(h for h in hits if h.rule_id == "FAKE-003")
    assert hit.category == "uncategorized"


def test_scan_text_benign_produces_no_hits(fake_scan_fn):
    scanner = Scanner(fake_scan_fn)
    assert scanner.scan_text("perfectly innocent text", file_label="a.md") == []


def test_hits_sorted_critical_first(fake_scan_fn):
    scanner = Scanner(fake_scan_fn)
    hits = scanner.scan_text("BADWORD and EXFIL", file_label="a.md")
    severities = [h.severity for h in hits]
    assert severities == sorted(
        severities,
        key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s, 9),
    )
    assert hits[0].severity == "critical"


def test_scan_paths_dedups_per_rule_per_file(fake_scan_fn, tmp_path):
    (tmp_path / "one.md").write_text("BADWORD", encoding="utf-8")
    (tmp_path / "two.md").write_text("BADWORD", encoding="utf-8")
    scanner = Scanner(fake_scan_fn)
    files = discover_manifest_files(tmp_path)
    hits = scanner.scan_paths(files, root=tmp_path)

    # Same rule fires for two distinct files -> two distinct rows.
    fake001 = [h for h in hits if h.rule_id == "FAKE-001"]
    assert {h.file for h in fake001} == {"one.md", "two.md"}


def test_rule_hit_to_row_is_all_strings(fake_scan_fn):
    scanner = Scanner(fake_scan_fn)
    hit = scanner.scan_text("BADWORD", file_label="a.md")[0]
    row = hit.to_row()
    assert set(row) == {
        "rule_id",
        "severity",
        "category",
        "file",
        "title",
        "confidence",
        "event_types",
        "matched_patterns",
    }
    assert all(isinstance(v, str) for v in row.values())


def test_discover_manifest_files_filters_suffixes(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "c.png").write_bytes(b"\x89PNG")
    found = {p.name for p in discover_manifest_files(tmp_path)}
    assert found == {"a.md", "b.json"}


def test_discover_manifest_files_single_file(tmp_path):
    f = tmp_path / "solo.md"
    f.write_text("x", encoding="utf-8")
    assert discover_manifest_files(f) == [f]


def test_discover_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover_manifest_files(tmp_path / "nope")


def test_scanner_rejects_bad_scan_fn():
    with pytest.raises(TypeError):
        Scanner(scan_fn="not callable")


def test_scanner_rejects_empty_event_types(fake_scan_fn):
    with pytest.raises(ValueError):
        Scanner(fake_scan_fn, event_types=[])


def test_read_tolerates_undecodable_bytes(fake_scan_fn, tmp_path):
    f = tmp_path / "weird.md"
    f.write_bytes(b"\xff\xfe BADWORD")
    scanner = Scanner(fake_scan_fn)
    hits = scanner.scan_paths(discover_manifest_files(tmp_path), root=tmp_path)
    assert any(h.rule_id == "FAKE-001" for h in hits)


def test_rule_hit_is_frozen():
    hit = RuleHit(
        rule_id="X",
        title="t",
        severity="low",
        category="c",
        file="f",
        confidence="high",
        event_types=(),
        matched_patterns=(),
    )
    with pytest.raises(Exception):
        hit.rule_id = "Y"  # type: ignore[misc]


# ---- build_pyatr_scan_fn guard (fake pyatr, so it runs without the engine) --

class _FakeEngine:
    def __init__(self, bundled: int = 0, from_dir: int = 0):
        self._bundled = bundled
        self._from_dir = from_dir
        self.loaded_from = []

    def load_bundled_rules(self):
        self.loaded_from.append("bundled")
        return self._bundled

    def load_rules_from_directory(self, directory):
        self.loaded_from.append(directory)
        return self._from_dir

    def evaluate(self, event):
        return []


def _install_fake_pyatr(monkeypatch, engine):
    import sys
    import types

    pyatr = types.ModuleType("pyatr")
    pyatr.ATREngine = lambda: engine
    pyatr.AgentEvent = lambda content, event_type: (content, event_type)
    monkeypatch.setitem(sys.modules, "pyatr", pyatr)


@pytest.mark.parametrize(
    ("engine", "rules_dir"),
    [(_FakeEngine(bundled=0), None), (_FakeEngine(from_dir=0), "/empty/rules")],
    ids=["bundled-rules-empty", "rules-dir-empty"],
)
def test_empty_rule_set_raises_instead_of_scanning(monkeypatch, engine, rules_dir):
    from atr_scanner.scanner import build_pyatr_scan_fn

    _install_fake_pyatr(monkeypatch, engine)
    with pytest.raises(ValueError, match="no ATR rules were loaded"):
        build_pyatr_scan_fn(rules_dir)


def test_loaded_rule_set_returns_a_scan_function(monkeypatch):
    from atr_scanner.scanner import build_pyatr_scan_fn

    engine = _FakeEngine(from_dir=3)
    _install_fake_pyatr(monkeypatch, engine)
    scan = build_pyatr_scan_fn("/custom/rules")
    assert engine.loaded_from == ["/custom/rules"]
    assert scan("text", "llm_input") == []
