"""Pure ATR scanning wrapper — no Dioptra dependency.

This module is the thin engine adapter. Given the *text* of an agent skill /
MCP manifest, it replays that text against the ATR engine across several
``event_type`` contexts and collapses the raw ``ATRMatch`` objects into a
flat, deduplicated hits table.

Design notes / contract:

* We replay the same manifest under multiple ``event_type`` values because an
  ATR rule only fires when the event context matches what it inspects (a
  ``tool_call`` rule never fires on an ``llm_input`` event). A skill manifest is
  static text with no runtime event context, so scanning it under every
  relevant context is the honest way to surface the full rule set that *could*
  match the text. See :data:`DEFAULT_EVENT_TYPES`.
* ``pyatr.ATRMatch`` has **no top-level ``category``** — the category lives in
  ``match.tags['category']``. We read it from there. (Verified against
  pyatr 0.2.6.)
* Hits are deduplicated per ``(rule_id, file)``. If a rule fires under more
  than one event_type for the same file we keep one row and record which
  event_types triggered it, so the table has one row per distinct finding.

The engine used here is ATR's Layer-1 regex engine (``pyatr``). It is a
pattern-matching floor, not a semantic analyzer — see README PROTOTYPE notes.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Iterable, Sequence

# Event contexts to replay each manifest under. A static manifest has no real
# runtime event; replaying across contexts surfaces every rule whose text
# pattern the manifest could trigger. Kept as a tuple = immutable default.
DEFAULT_EVENT_TYPES: tuple[str, ...] = (
    "llm_input",
    "tool_call",
    "tool_response",
    "llm_output",
    "system",
)

# File extensions treated as scannable manifest text.
MANIFEST_SUFFIXES: tuple[str, ...] = (
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
)

UNCATEGORIZED = "uncategorized"


@dataclasses.dataclass(frozen=True)
class RuleHit:
    """One deduplicated finding: a rule that fired against one file.

    Immutable by design (frozen dataclass) — callers build new objects rather
    than mutating, matching the project immutability rule.
    """

    rule_id: str
    title: str
    severity: str
    category: str
    file: str
    confidence: str
    event_types: tuple[str, ...]
    matched_patterns: tuple[str, ...]

    def to_row(self) -> dict[str, str]:
        """Flatten to a JSON/CSV-friendly row (all scalar strings)."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "title": self.title,
            "confidence": self.confidence,
            "event_types": ",".join(self.event_types),
            "matched_patterns": " | ".join(self.matched_patterns),
        }


# Severity ordering for deterministic, meaningful sort (critical first).
_SEVERITY_ORDER = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}


def _category_of(match) -> str:
    """Extract category from an ATRMatch.

    pyatr's ATRMatch has no ``category`` attribute; the category is stored in
    ``tags['category']``. We defensively fall back to any top-level attribute
    (in case a future pyatr adds one) and finally to ``uncategorized``.
    """
    tags = getattr(match, "tags", None) or {}
    category = tags.get("category")
    if not category:
        category = getattr(match, "category", None)
    return category or UNCATEGORIZED


class Scanner:
    """Wraps a *scan callable* so tests can inject a fake engine.

    ``scan_fn`` must have the signature ``(text: str, event_type: str) ->
    list[match]`` where each *match* exposes ``rule_id``, ``title``,
    ``severity``, ``confidence``, ``matched_patterns`` and ``tags``. In
    production this is backed by :func:`build_pyatr_scan_fn`; in unit tests it
    is a fake with zero external dependencies.
    """

    def __init__(
        self,
        scan_fn,
        event_types: Sequence[str] = DEFAULT_EVENT_TYPES,
    ) -> None:
        if not callable(scan_fn):
            raise TypeError("scan_fn must be callable")
        if not event_types:
            raise ValueError("event_types must not be empty")
        self._scan_fn = scan_fn
        self._event_types = tuple(event_types)

    def scan_text(self, text: str, *, file_label: str) -> list[RuleHit]:
        """Scan one manifest's text; return deduplicated hits for that file."""
        if text is None:
            raise ValueError("text must not be None")

        # Accumulate per rule_id: which event_types fired + first match seen.
        by_rule: dict[str, dict] = {}
        for event_type in self._event_types:
            for match in self._scan_fn(text, event_type):
                rule_id = getattr(match, "rule_id", None)
                if not rule_id:
                    continue
                entry = by_rule.get(rule_id)
                if entry is None:
                    by_rule[rule_id] = {
                        "match": match,
                        "event_types": [event_type],
                    }
                elif event_type not in entry["event_types"]:
                    entry["event_types"].append(event_type)

        hits = [
            RuleHit(
                rule_id=rule_id,
                title=getattr(entry["match"], "title", "") or "",
                severity=(getattr(entry["match"], "severity", "") or "").lower(),
                category=_category_of(entry["match"]),
                file=file_label,
                confidence=getattr(entry["match"], "confidence", "") or "",
                event_types=tuple(entry["event_types"]),
                matched_patterns=tuple(
                    getattr(entry["match"], "matched_patterns", ()) or ()
                ),
            )
            for rule_id, entry in by_rule.items()
        ]
        return _sort_hits(hits)

    def scan_paths(self, paths: Iterable[Path], *, root: Path) -> list[RuleHit]:
        """Scan a set of manifest files; return combined deduplicated hits.

        Dedup is per ``(rule_id, file)`` — the same rule may legitimately fire
        against different files and each is a distinct finding.
        """
        all_hits: list[RuleHit] = []
        for path in paths:
            text = _read_text(path)
            label = _relative_label(path, root)
            all_hits.extend(self.scan_text(text, file_label=label))
        return _sort_hits(all_hits)


def _sort_hits(hits: list[RuleHit]) -> list[RuleHit]:
    """Deterministic sort: severity (critical first), then file, then rule_id."""
    return sorted(
        hits,
        key=lambda h: (
            _SEVERITY_ORDER.get(h.severity, 99),
            h.file,
            h.rule_id,
        ),
    )


def _read_text(path: Path) -> str:
    """Read manifest text, tolerating undecodable bytes rather than crashing."""
    return path.read_text(encoding="utf-8", errors="replace")


def _relative_label(path: Path, root: Path) -> str:
    """Human-readable file label relative to the scanned root."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def discover_manifest_files(root: Path) -> list[Path]:
    """Find scannable manifest files under ``root`` (recursively, sorted).

    A single file path is returned as-is. Validation of existence is the
    caller's job (see tasks.resolve_input), but we fail fast on a bad root.
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"manifest root does not exist: {root}")
    if root.is_file():
        return [root]
    files = [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix.lower() in MANIFEST_SUFFIXES
    ]
    return files


def build_pyatr_scan_fn(rules_dir: str | Path | None = None):
    """Build a production scan callable backed by the real pyatr engine.

    Imported lazily so unit tests never require pyatr to be installed.
    Returns a callable ``(text, event_type) -> list[ATRMatch]``.
    """
    from pyatr import ATREngine, AgentEvent  # noqa: PLC0415 (lazy on purpose)

    engine = ATREngine()
    if rules_dir is not None:
        engine.load_rules_from_directory(str(rules_dir))
    else:
        engine.load_bundled_rules()

    def _scan(text: str, event_type: str):
        event = AgentEvent(content=text, event_type=event_type)
        return engine.evaluate(event)

    return _scan
