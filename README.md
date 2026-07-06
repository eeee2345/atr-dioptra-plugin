# ATR scanner plugin for NIST Dioptra

> **Status: PROTOTYPE.** This is an early, self-managed plugin that wraps the
> ATR (Agent Threat Rules) engine as a Dioptra task. The scanning core and the
> local CLI are real and tested; the Dioptra packaging has **not yet been
> round-tripped through a live Dioptra deployment**. See
> [PROTOTYPE gaps](#prototype-gaps) before relying on it.

A thin Dioptra plugin that runs the ATR Layer-1 engine (`pyatr`) over a
directory (or S3 prefix) of agent **skill / MCP manifests** and emits a
**rule-hits table** as Dioptra artifacts plus summary metrics.

This plugin lives in its own repository (not a Dioptra fork), per the guidance
from Dioptra maintainer `jkglasbrenner` on
[usnistgov/dioptra#1283](https://github.com/usnistgov/dioptra/issues/1283):
plugins should be self-managed and imported into Dioptra via the `dioptra.toml`
resource-import format.

## What it does

```
manifests dir / S3 prefix
        │
        ▼
  resolve_input      → local path
        │
        ▼
  scan_manifests     → run ATR engine across event contexts, dedup per rule/file
        │
        ├─► write_hits_artifact  → atr_hits.json + atr_hits.csv  (Dioptra artifact)
        └─► summarize_hits        → metrics (total / per-severity / per-category)
```

## Layout

| Path | Purpose |
|------|---------|
| `dioptra.toml` | Dioptra resource-import manifest (plugins / param types / entrypoints) |
| `plugins/atr_scanner/scanner.py` | Pure scan core — **no Dioptra dependency**, fully unit-tested |
| `plugins/atr_scanner/tasks.py` | Dioptra task entrypoints declared in `dioptra.toml` |
| `plugins/atr_scanner/cli.py` | Standalone CLI (`atr-dioptra-scan`) |
| `entrypoints/atr-scan.yaml` | Task-graph wiring the tasks together |
| `Dockerfile` | Custom Dioptra worker image (layers ATR deps on the worker base) |
| `examples/sample_manifests/` | 1 benign + 1 malicious sample |
| `tests/` | Unit (fake engine, no deps) + integration smoke (real `pyatr`) |

## Install & run locally

Requires **Python ≥ 3.10** (a `pyatr` constraint).

```bash
pip install -e ".[dev]"
pytest -q

# Scan the bundled samples
atr-dioptra-scan --input examples/sample_manifests --output output
# → writes output/atr_hits.json and output/atr_hits.csv, prints metrics
```

Use `--fail-on critical` (or `high`/`medium`/`low`) to make the CLI a CI gate
that exits non-zero when a hit at or above that severity is found.

## Install into Dioptra

1. **Build the custom worker image.** The plugin needs the ATR runtime
   (`pyatr`) importable inside the worker. Edit `Dockerfile` and replace the
   placeholder `DIOPTRA_WORKER_BASE` with the real Dioptra worker base image
   tag for your deployment, then:

   ```bash
   docker build \
     --build-arg DIOPTRA_WORKER_BASE=ghcr.io/usnistgov/dioptra/<worker>:<tag> \
     -t atr-dioptra-worker:0.1.0 .
   ```

   Register that image as the worker for the ATR jobs.

2. **Import the resources.** Point Dioptra's resource import at `dioptra.toml`.
   It registers:
   - the `atr_scanner` plugin and its 5 tasks,
   - the `atr_hits_table` and `atr_metrics` custom parameter types,
   - the `ATR Manifest Scan` entrypoint (`entrypoints/atr-scan.yaml`).

3. **Run the entrypoint**, passing `manifest_location` (a path reachable by the
   worker, or an `s3://` URI) and `output_dir`.

## Input / output contract

**Input** — one of:
- a local directory of manifests (scanned recursively; files with suffixes
  `.md .txt .json .yaml .yml .toml`),
- a single local file, or
- an `s3://bucket/prefix` URI (requires the `[s3]` extra / `boto3` in the
  worker, plus a `s3_download_dir`).

**Output** — a hits table, one row per **(rule_id, file)**, with columns:

| column | meaning |
|--------|---------|
| `rule_id` | ATR rule id, e.g. `ATR-2026-00113` |
| `severity` | `critical` / `high` / `medium` / `low` |
| `category` | ATR category (read from the match's `tags['category']`) |
| `file` | manifest file, relative to the scanned root |
| `title` | human-readable rule title |
| `confidence` | rule confidence |
| `event_types` | which replay contexts fired the rule (comma-separated) |
| `matched_patterns` | matched pattern excerpts |

Emitted as `atr_hits.json` and `atr_hits.csv`. Metrics: `total_hits`,
`files_with_findings`, per-severity counts, and `category_breakdown`.

### Why replay across event types

A skill/MCP manifest is static text with no runtime event context, but ATR
rules only fire when the event context matches what the rule inspects (a
`tool_call` rule never matches an `llm_input` event). So the scanner replays
each manifest under several contexts (`llm_input`, `tool_call`,
`tool_response`, `llm_output`, `system`) and deduplicates, recording which
contexts triggered each rule. This surfaces the full set of rules whose
patterns the manifest text could trigger.

## PROTOTYPE gaps

Honest limitations — do **not** present this as production-verified:

- **The custom worker container has not been built or run against a live
  Dioptra worker.** The `Dockerfile` uses a placeholder base image
  (`python:3.12-slim`) that must be swapped for the real Dioptra worker base;
  the worker-queue integration is untested.
- **`dioptra.toml` has not been round-tripped through a live Dioptra resource
  import.** Field names/nesting match the published
  `usnistgov/dioptra/extra/dioptra.toml` example and the resource-import
  reference, but no running Dioptra instance has validated this specific file.
  An always-on test (`tests/test_dioptra_toml.py`) reconciles the TOML against
  the code, but that is not the same as a live import.
- **`entrypoints/atr-scan.yaml` graph wiring is modeled on the example
  entrypoints**, not verified by execution inside Dioptra.
- **The ATR engine here is `pyatr` — a Layer-1 *regex* engine.** It is a
  pattern-matching floor, not a semantic analyzer. It will miss obfuscated or
  novel attacks and can false-positive on benign text that resembles an attack
  pattern. Treat hits as signals to triage, not verdicts. (Concrete example
  found while building the benign sample: some rules lack word boundaries, so
  the innocuous word "Skill" matched a `kill|shutdown|...` pattern, and a `##
  Instructions` markdown heading matched a system-prompt-override rule. Layer-1
  precision is lane-dependent — see the ATR precision notes, not a single
  headline number.)
- **S3 input is implemented but untested against real S3** (boto3 path is
  import-lazy and only exercised on `s3://` inputs).

## License

MIT.
