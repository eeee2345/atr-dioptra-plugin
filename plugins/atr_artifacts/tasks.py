"""Artifact task that stores the ATR hits table as a Dioptra artifact.

The entrypoint's ``artifacts:`` block hands the output of the scan step to
:class:`AtrHitsTableArtifactTask`. Dioptra calls ``serialize`` after the task
graph finishes, uploads the returned file to its artifact store and registers
it on the job; ``deserialize`` lets a later job take the table back as an
artifact parameter.

Formats:

* ``json`` — the rows as a JSON list (lossless round trip).
* ``csv``  — one row per (rule_id, file) with a fixed header, so an empty scan
  still produces a well-formed file.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

try:
    from dioptra.sdk.api.artifact import ArtifactTaskInterface
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
    if exc.name != "dioptra":
        raise
    # Outside a Dioptra worker (unit tests) the task is a plain class. Inside a
    # worker it must subclass the real interface: Dioptra skips artifact tasks
    # that do not.
    ArtifactTaskInterface = object  # type: ignore[assignment,misc]

# Column order of the CSV form. Must match atr_scanner.tasks._CSV_COLUMNS (a
# test reconciles the two; this plugin cannot import atr_scanner).
HITS_TABLE_COLUMNS: tuple[str, ...] = (
    "rule_id",
    "severity",
    "category",
    "file",
    "title",
    "confidence",
    "event_types",
    "matched_patterns",
)

SUPPORTED_FORMATS: tuple[str, ...] = ("json", "csv")

# Leading characters a spreadsheet would evaluate as a formula. Manifest file
# names are untrusted input, so CSV cells starting with one are neutralised.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class UnsupportedHitsTableFormatError(ValueError):
    """The requested hits-table file format is not supported."""


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_CSV_FORMULA_PREFIXES) else text


def _check_format(format: str) -> str:
    if format not in SUPPORTED_FORMATS:
        raise UnsupportedHitsTableFormatError(
            f"Unsupported hits table format {format!r}; "
            f"expected one of {SUPPORTED_FORMATS}"
        )
    return format


class AtrHitsTableArtifactTask(ArtifactTaskInterface):
    """Serialize / deserialize the ``atr_hits_table`` parameter type."""

    @staticmethod
    def serialize(
        working_dir: Path,
        name: str,
        contents: list[dict[str, Any]],
        format: str = "json",
        **kwargs,
    ) -> Path:
        _check_format(format)
        if not isinstance(contents, list):
            raise TypeError(
                f"hits table must be a list of rows, got {type(contents).__name__}"
            )

        # Not with_suffix(): it would cut a dotted artifact name short.
        path = Path(working_dir) / f"{name}.{format}"
        if format == "json":
            path.write_text(json.dumps(contents, indent=2), encoding="utf-8")
            return path

        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(HITS_TABLE_COLUMNS))
            writer.writeheader()
            for row in contents:
                writer.writerow({col: _csv_cell(row.get(col)) for col in HITS_TABLE_COLUMNS})
        return path

    @staticmethod
    def deserialize(working_dir: Path, path: str, **kwargs) -> list[dict[str, Any]]:
        source = Path(working_dir, path)
        format = _check_format(source.suffix.lstrip("."))
        if format == "json":
            rows = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise TypeError(f"{source.name} does not contain a list of rows")
            return rows

        with source.open(newline="", encoding="utf-8") as fh:
            return [dict(row) for row in csv.DictReader(fh)]

    @staticmethod
    def validation() -> dict[str, Any] | None:
        return {"format": {"enum": list(SUPPORTED_FORMATS)}}
