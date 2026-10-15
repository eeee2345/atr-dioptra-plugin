# Custom Dioptra worker image for the ATR scanner plugin.
#
# A Dioptra plugin runs inside a "worker" container. This image layers the ATR
# runtime (pyatr + its bundled rules) onto an official Dioptra worker image, so
# it keeps that worker's entrypoint, healthcheck, unprivileged user and
# /opt/venv unchanged. Only the runtime dependency is added: the plugin code is
# NOT installed into the venv, because Dioptra ships the imported plugin to the
# worker with every job (an installed copy could shadow the imported one).
#
# Built and run against ghcr.io/usnistgov/dioptra/pytorch-cpu:1.2.1 (see
# README "Validation status"). Any official worker image works as the base;
# keep its tag equal to your deployment's container_tag:
#   docker build --build-arg DIOPTRA_WORKER_BASE=ghcr.io/usnistgov/dioptra/tensorflow2-cpu:1.2.1 ...
ARG DIOPTRA_WORKER_BASE=ghcr.io/usnistgov/dioptra/pytorch-cpu:1.2.1
# The official worker images build /opt/venv with uv but do not ship it; borrow
# the same uv release for the install step without copying it into the image.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.4

FROM ${UV_IMAGE} AS uv

FROM ${DIOPTRA_WORKER_BASE}

# ---- ATR runtime dependency ------------------------------------------------
# pyatr ships the ATR Layer-1 rules bundled, so no rules volume is required for
# the default scan. Pinned for reproducibility. The base image runs as the
# dioptra user (uid 39000), which owns /opt/venv.
ARG PYATR_VERSION=0.3.0
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv pip install --python /opt/venv/bin/python --no-cache "pyatr==${PYATR_VERSION}"

# ---- Build-time smoke test --------------------------------------------------
# Scan the bundled samples with the plugin code so a broken image fails fast.
# The copy lives outside the worker's import path and is only used here (and
# for manual debugging with PYTHONPATH=/opt/atr-dioptra-plugin/plugins).
COPY plugins /opt/atr-dioptra-plugin/plugins
COPY examples /opt/atr-dioptra-plugin/examples
RUN cd /opt/atr-dioptra-plugin \
    && PYTHONPATH=plugins python -m atr_scanner.cli \
         --input examples/sample_manifests --output /tmp/atr-smoke \
    && python -c "import json, sys; rows = json.load(open('/tmp/atr-smoke/atr_hits.json')); files = {r['file'] for r in rows}; sys.exit(0 if rows and not any('benign' in f for f in files) else 'smoke test: unexpected hits table')"

# ENTRYPOINT, CMD, USER, WORKDIR and HEALTHCHECK script are inherited from the
# Dioptra worker base image; pass the queue name via docker compose `command:`.
