"""Standalone CLI for the ATR scanner plugin.

Lets you run the same scan the Dioptra task-graph runs, without a Dioptra
deployment — useful for local dev, CI smoke tests, and demoing the input/output
contract.

    python -m atr_scanner.cli --input examples/sample_manifests --output out
    atr-dioptra-scan --input examples/sample_manifests --output out
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .tasks import atr_scan_and_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atr-dioptra-scan",
        description=(
            "Scan agent skill / MCP manifests with the ATR engine and emit a "
            "rule-hits table (JSON + CSV) plus summary metrics."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Local directory/file path or s3://bucket/prefix of manifests.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Directory to write atr_hits.json / atr_hits.csv into.",
    )
    parser.add_argument(
        "--rules-dir",
        default=None,
        help="Optional path to a custom ATR rules directory (defaults to the "
        "bundled pyatr rules).",
    )
    parser.add_argument(
        "--s3-download-dir",
        default=None,
        help="Local scratch dir for s3:// inputs (required only for S3).",
    )
    parser.add_argument(
        "--fail-on",
        default=None,
        choices=["critical", "high", "medium", "low"],
        help="Exit non-zero if any hit at or above this severity is found "
        "(useful as a CI gate).",
    )
    return parser


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _gate_failed(metrics: dict, fail_on: str | None) -> bool:
    if not fail_on:
        return False
    threshold = _SEVERITY_RANK[fail_on]
    breakdown = metrics.get("severity_breakdown", {})
    return any(
        _SEVERITY_RANK.get(sev, 99) <= threshold and count > 0
        for sev, count in breakdown.items()
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = atr_scan_and_report(
            manifest_location=args.input,
            output_dir=args.output,
            rules_dir=args.rules_dir,
            s3_download_dir=args.s3_download_dir,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    metrics = result["metrics"]
    print(f"Scanned input: {args.input}")
    print(f"Artifact written: {result['artifact_path']}")
    print(f"  also: {Path(result['artifact_path']).with_name('atr_hits.csv')}")
    print("Metrics:")
    print(json.dumps(metrics, indent=2))

    if _gate_failed(metrics, args.fail_on):
        print(
            f"gate: found hit(s) at or above severity '{args.fail_on}'",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
