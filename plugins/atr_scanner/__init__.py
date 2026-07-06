"""ATR scanner plugin for NIST Dioptra.

Thin wrapper that runs the ATR (Agent Threat Rules) Layer-1 engine over a
directory (or S3 prefix) of agent skill / MCP manifests and emits a rule-hits
table as Dioptra artifacts + metrics.

Public surface:

* :mod:`scanner`  — pure, Dioptra-free scanning core.
* :mod:`tasks`    — Dioptra task entrypoints declared in ``dioptra.toml``.
* :mod:`cli`      — standalone command-line runner.
"""

from __future__ import annotations

from .scanner import (
    DEFAULT_EVENT_TYPES,
    RuleHit,
    Scanner,
    build_pyatr_scan_fn,
    discover_manifest_files,
)
from .tasks import (
    PARAM_TYPE_HITS_TABLE,
    PARAM_TYPE_METRICS,
    TASK_ATR_SCAN_AND_REPORT,
    TASK_RESOLVE_INPUT,
    TASK_SCAN_MANIFESTS,
    TASK_SUMMARIZE_HITS,
    TASK_WRITE_HITS_ARTIFACT,
    atr_scan_and_report,
    resolve_input,
    scan_manifests,
    summarize_hits,
    write_hits_artifact,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_EVENT_TYPES",
    "RuleHit",
    "Scanner",
    "build_pyatr_scan_fn",
    "discover_manifest_files",
    "resolve_input",
    "scan_manifests",
    "write_hits_artifact",
    "summarize_hits",
    "atr_scan_and_report",
    "TASK_RESOLVE_INPUT",
    "TASK_SCAN_MANIFESTS",
    "TASK_WRITE_HITS_ARTIFACT",
    "TASK_SUMMARIZE_HITS",
    "TASK_ATR_SCAN_AND_REPORT",
    "PARAM_TYPE_HITS_TABLE",
    "PARAM_TYPE_METRICS",
]
