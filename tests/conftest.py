"""Shared test fixtures.

The unit tests use a *fake* scan engine with zero external dependencies, so
they run without pyatr installed. Only the integration smoke test imports the
real pyatr engine.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass(frozen=True)
class FakeMatch:
    """Mimics pyatr.ATRMatch — crucially, category lives in ``tags``."""

    rule_id: str
    title: str
    severity: str
    confidence: str = "high"
    matched_patterns: tuple[str, ...] = ()
    tags: dict = dataclasses.field(default_factory=dict)


def make_fake_scan_fn(rules_by_event_type):
    """Build a fake scan_fn: (text, event_type) -> list[FakeMatch].

    ``rules_by_event_type`` maps an event_type to the list of FakeMatch objects
    that should fire when a trigger substring is present in the text. Each entry
    is (trigger_substring, FakeMatch). If trigger is None the match always
    fires for that event_type.
    """

    def _scan(text: str, event_type: str):
        out = []
        for trigger, match in rules_by_event_type.get(event_type, []):
            if trigger is None or trigger in text:
                out.append(match)
        return out

    return _scan


@pytest.fixture
def fake_scan_fn():
    """A fake engine: 'BADWORD' triggers a critical rule under two contexts."""
    critical = FakeMatch(
        rule_id="FAKE-001",
        title="Fake Critical Rule",
        severity="critical",
        confidence="high",
        matched_patterns=("BADWORD",),
        tags={"category": "prompt-injection"},
    )
    high = FakeMatch(
        rule_id="FAKE-002",
        title="Fake High Rule",
        severity="high",
        confidence="medium",
        matched_patterns=("EXFIL",),
        tags={"category": "context-exfiltration"},
    )
    # no-category rule to exercise the uncategorized fallback
    nocat = FakeMatch(
        rule_id="FAKE-003",
        title="Fake No-Category Rule",
        severity="low",
        tags={},
    )
    return make_fake_scan_fn(
        {
            "llm_input": [("BADWORD", critical), ("NOCAT", nocat)],
            "tool_call": [("BADWORD", critical)],  # same rule, 2nd context
            "tool_response": [("EXFIL", high)],
        }
    )


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
