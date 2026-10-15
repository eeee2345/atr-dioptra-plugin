"""Unit tests for the hits-table artifact task (no Dioptra install needed)."""

from __future__ import annotations

import csv
import json

import pytest

from atr_artifacts.tasks import (
    HITS_TABLE_COLUMNS,
    AtrHitsTableArtifactTask,
    UnsupportedHitsTableFormatError,
)

HITS = [
    {
        "rule_id": "ATR-TEST-0001",
        "severity": "critical",
        "category": "prompt-injection",
        "file": "skills/a.md",
        "title": "Test rule",
        "confidence": "high",
        "event_types": "llm_input,tool_call",
        "matched_patterns": "ignore previous | exfiltrate",
    }
]


def test_json_round_trip(tmp_path):
    path = AtrHitsTableArtifactTask.serialize(tmp_path, "atr_hits", HITS, format="json")
    assert path.name == "atr_hits.json"
    assert json.loads(path.read_text(encoding="utf-8")) == HITS
    assert AtrHitsTableArtifactTask.deserialize(tmp_path, path.name) == HITS


def test_csv_round_trip(tmp_path):
    path = AtrHitsTableArtifactTask.serialize(tmp_path, "atr_hits_csv", HITS, format="csv")
    assert path.name == "atr_hits_csv.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames) == HITS_TABLE_COLUMNS
    assert AtrHitsTableArtifactTask.deserialize(tmp_path, path.name) == HITS


def test_empty_table_still_has_csv_header(tmp_path):
    path = AtrHitsTableArtifactTask.serialize(tmp_path, "empty", [], format="csv")
    assert path.read_text(encoding="utf-8").strip() == ",".join(HITS_TABLE_COLUMNS)
    assert AtrHitsTableArtifactTask.deserialize(tmp_path, path.name) == []


def test_default_format_is_json(tmp_path):
    path = AtrHitsTableArtifactTask.serialize(tmp_path, "atr_hits", HITS)
    assert path.suffix == ".json"


def test_unsupported_format_is_rejected(tmp_path):
    with pytest.raises(UnsupportedHitsTableFormatError):
        AtrHitsTableArtifactTask.serialize(tmp_path, "atr_hits", HITS, format="xlsx")


def test_non_list_contents_are_rejected(tmp_path):
    with pytest.raises(TypeError):
        AtrHitsTableArtifactTask.serialize(tmp_path, "atr_hits", {"rows": HITS})


def test_validation_schema_lists_supported_formats():
    assert AtrHitsTableArtifactTask.validation() == {"format": {"enum": ["json", "csv"]}}


def test_csv_neutralises_spreadsheet_formulas(tmp_path):
    rows = [{**HITS[0], "file": "=cmd|'/c calc'!A1.md", "title": "@SUM(1)"}]
    path = AtrHitsTableArtifactTask.serialize(tmp_path, "atr_hits_csv", rows, format="csv")
    loaded = AtrHitsTableArtifactTask.deserialize(tmp_path, path.name)[0]
    assert loaded["file"] == "'=cmd|'/c calc'!A1.md"
    assert loaded["title"] == "'@SUM(1)"
    # JSON keeps exact values; it is the lossless form.
    json_path = AtrHitsTableArtifactTask.serialize(tmp_path, "atr_hits", rows, format="json")
    assert AtrHitsTableArtifactTask.deserialize(tmp_path, json_path.name) == rows


def test_dotted_artifact_names_are_kept_whole(tmp_path):
    path = AtrHitsTableArtifactTask.serialize(tmp_path, "atr.hits.v2", HITS, format="json")
    assert path.name == "atr.hits.v2.json"
