# Custom Dioptra worker image for the ATR scanner plugin.
#
# A Dioptra plugin runs inside a "worker" container. To make the ATR runtime
# (pyatr + its bundled rules) importable by the plugin tasks, we layer those
# deps on top of the official Dioptra worker base image.
#
# PROTOTYPE / TODO — replace this placeholder base with the REAL Dioptra worker
# base image tag. As of writing, the exact published tag must be confirmed from
# the Dioptra release you are targeting, e.g. one of:
#   FROM ghcr.io/usnistgov/dioptra/tensorflow2-cpu:<version>
#   FROM ghcr.io/usnistgov/dioptra/pytorch-cpu:<version>
# (Confirm against https://github.com/usnistgov/dioptra packages before build.)
# This has NOT been built or run against a live Dioptra worker yet.
ARG DIOPTRA_WORKER_BASE=python:3.12-slim
FROM ${DIOPTRA_WORKER_BASE}

# ---- ATR runtime dependencies ----------------------------------------------
# pyatr ships the ATR Layer-1 rules bundled, so no rules volume is required for
# the default scan. Pin a known-good version for reproducibility.
ARG PYATR_VERSION=0.2.6
RUN pip install --no-cache-dir "pyatr==${PYATR_VERSION}"

# ---- Plugin code -----------------------------------------------------------
# In a real Dioptra deployment the plugin is imported via `dioptra.toml`
# resource import rather than baked in, but we install the package here so the
# worker can `import atr_scanner` directly and so the image is self-testing.
WORKDIR /opt/atr-dioptra-plugin
COPY pyproject.toml README.md ./
COPY plugins ./plugins
COPY entrypoints ./entrypoints
COPY dioptra.toml ./dioptra.toml
COPY examples ./examples
RUN pip install --no-cache-dir .

# Smoke-test the CLI at build time so a broken image fails fast.
RUN atr-dioptra-scan --input examples/sample_manifests --output /tmp/atr-out \
    && test -f /tmp/atr-out/atr_hits.json

# The Dioptra worker base image already defines the correct ENTRYPOINT/CMD to
# join the Dioptra job queue; we intentionally do NOT override it here. When
# using the placeholder base above, invoke the CLI manually instead.
