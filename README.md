# ATR scanner plugin for NIST Dioptra

> **Status: early.** See [Validation status](#validation-status) for what has
> and has not been exercised against a live Dioptra deployment before relying
> on it.

A thin Dioptra plugin that runs the ATR Layer-1 engine (`pyatr`) over a
directory (or S3 prefix) of agent **skill / MCP manifests** and records a
**rule-hits table** as Dioptra artifacts plus summary metrics on the job.

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
        ├─► AtrHitsTableArtifactTask → atr_hits.json + atr_hits_csv.csv (job artifacts)
        └─► summarize_hits → log_scan_metrics → job metrics (totals, per severity)
```

## Layout

| Path | Purpose |
|------|---------|
| `dioptra.toml` | Dioptra resource-import manifest (plugins / param types / entrypoints) |
| `plugins/atr_scanner/scanner.py` | Pure scan core — **no Dioptra dependency**, fully unit-tested |
| `plugins/atr_scanner/tasks.py` | Dioptra function tasks declared in `dioptra.toml` |
| `plugins/atr_scanner/cli.py` | Standalone CLI (`atr-dioptra-scan`) |
| `plugins/atr_artifacts/tasks.py` | Dioptra artifact task that stores the hits table (JSON / CSV) |
| `entrypoints/atr-scan.yaml` | Task graph + artifact outputs of the "ATR Manifest Scan" entrypoint |
| `Dockerfile` | Custom Dioptra worker image (adds `pyatr` to an official worker image) |
| `examples/sample_manifests/` | 1 benign + 1 malicious sample |
| `scripts/` | Live end-to-end run against a Dioptra deployment (also used by CI) |
| `tests/` | Unit (fake engine), reconciliation, Dioptra compatibility, integration (real `pyatr`) |

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

These steps follow the Dioptra guides for
[custom workers](https://pages.nist.gov/dioptra/how-to/setup-dioptra/configure-setup/creating-custom-workers.html),
[integrating custom containers](https://pages.nist.gov/dioptra/how-to/setup-dioptra/configure-setup/integrating-custom-containers.html)
and [importing resources](https://pages.nist.gov/dioptra/how-to/import-content/import-resources.html).
`scripts/e2e-dioptra.sh` performs all of them against a deployment.

1. **Build the worker image** on the official worker image whose tag matches
   your deployment's `container_tag`. Only `pyatr` is added; the entrypoint,
   healthcheck and user of the official image are kept. The plugin code itself
   is not baked in: Dioptra ships the imported plugin to the worker with every
   job.

   ```bash
   docker build \
     --build-arg DIOPTRA_WORKER_BASE=ghcr.io/usnistgov/dioptra/pytorch-cpu:1.2.1 \
     -t atr-dioptra-worker:1.2.1 .
   ```

2. **Add the worker to the deployment** in `docker-compose.override.yml`
   (replace `<deployment>` with the deployment's slug and `<host-data-path>`
   with the host directory that holds the manifests), then `docker compose up -d`:

   ```yaml
   services:
     <deployment>-atr-worker:
       image: atr-dioptra-worker:1.2.1
       restart: always
       hostname: <deployment>-atr-worker
       healthcheck:
         test: ["CMD", "/usr/local/bin/healthcheck.sh"]
         interval: 30s
         timeout: 60s
         retries: 5
         start_period: 80s
       environment:
         AWS_ACCESS_KEY_ID: ${WORKER_AWS_ACCESS_KEY_ID}
         AWS_SECRET_ACCESS_KEY: ${WORKER_AWS_SECRET_ACCESS_KEY}
         DIOPTRA_WORKER_USERNAME: ${DIOPTRA_WORKER_USERNAME}
         DIOPTRA_WORKER_PASSWORD: ${DIOPTRA_WORKER_PASSWORD}
       command:
         - --wait-for
         - <deployment>-redis:6379
         - --wait-for
         - <deployment>-minio:9001
         - --wait-for
         - <deployment>-db:5432
         - --wait-for
         - <deployment>-mlflow-tracking:5000
         - --wait-for
         - <deployment>-restapi:5000
         - atr-scanner            # the queue this worker serves
       env_file:
         - ./envs/ca-certificates.env
         - ./envs/<deployment>-worker.env
         - ./envs/<deployment>-worker-cpu.env
       networks:
         - dioptra
       volumes:
         - "worker-ca-certificates:/usr/local/share/ca-certificates:rw"
         - "worker-etc-ssl:/etc/ssl:rw"
         - "<host-data-path>:/dioptra/data:ro"
   ```

3. **Import the resources** from `https://github.com/eeee2345/atr-dioptra-plugin`
   (config path `dioptra.toml`), in the GUI or with the Python client:

   ```python
   client.workflows.import_resources(
       group_id=1, source="https://github.com/eeee2345/atr-dioptra-plugin"
   )
   ```

   This registers the `atr_scanner` plugin (function tasks), the
   `atr_artifacts` plugin (the hits-table artifact task), the `atr_hits_table`
   and `atr_metrics` parameter types, and the `ATR Manifest Scan` entrypoint.

4. **Create a queue named `atr-scanner`**, attach it to the entrypoint, and run
   a job with `manifest_location` set to a path under `/dioptra/data` (or an
   `s3://` URI plus `s3_download_dir`).

## Input / output contract

**Input** — the entrypoint parameter `manifest_location`, one of:
- a directory of manifests reachable by the worker (scanned recursively; files
  with suffixes `.md .txt .json .yaml .yml .toml`), typically under the
  deployment's data mount `/dioptra/data`,
- a single file, or
- an `s3://bucket/prefix` URI (requires `boto3` in the worker and a
  `s3_download_dir`).

`rules_dir` (optional) points at a custom ATR rules directory; left empty, the
rules bundled with `pyatr` are used. A rule set that loads no rules is an
error, never a clean scan.

**Output** — recorded on the Dioptra job:

- **Artifacts** `atr_hits` (`atr_hits.json`) and `atr_hits_csv`
  (`atr_hits_csv.csv`): the hits table, one row per **(rule_id, file)**:

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

- **Metrics** `total_hits`, `files_scanned`, `files_with_findings`,
  `critical`, `high`, `medium`, `low`. The per-category breakdown is part of
  the `summarize_hits` output (and derivable from the artifact).

The CLI writes the same table as `atr_hits.json` / `atr_hits.csv` and prints
the metrics.

### Why replay across event types

A skill/MCP manifest is static text with no runtime event context, but ATR
rules only fire when the event context matches what the rule inspects (a
`tool_call` rule never matches an `llm_input` event). So the scanner replays
each manifest under several contexts (`llm_input`, `tool_call`,
`tool_response`, `llm_output`, `system`) and deduplicates, recording which
contexts triggered each rule. This surfaces the full set of rules whose
patterns the manifest text could trigger.

## Testing

| Command | What it checks |
|---------|----------------|
| `pytest -q` | Unit tests (fake engine), the `dioptra.toml` ↔ code ↔ entrypoint reconciliation, and the integration smoke with the real `pyatr` |
| `pip install dioptra-platform==1.2.1` then `pytest -q` | Adds `tests/test_dioptra_compat.py`: the TOML against Dioptra's own import schema, `pyplugs` registration of every task, the artifact task interface |
| `scripts/setup-dioptra.sh <dir>` then `scripts/e2e-dioptra.sh <dir>/atr-e2e` | Live round trip: fresh deployment from the official template, worker build, resource import of `HEAD`, one job, checks on the job's status, metrics and artifacts |

CI (`.github/workflows/ci.yml`) runs the first two plus the worker image
build on every push; `.github/workflows/dioptra-e2e.yml` runs the live round
trip on demand and weekly, to catch Dioptra changes early.

## Validation status

Pending: the live round trip against Dioptra 1.2.1 (`scripts/e2e-dioptra.sh`).

## License

MIT.
