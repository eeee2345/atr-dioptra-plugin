#!/usr/bin/env bash
# Build the ATR worker, add it to a running Dioptra deployment, import this
# repository's resources and run one scan job end to end.
#
# Follows the Dioptra docs for custom workers:
#   https://pages.nist.gov/dioptra/how-to/setup-dioptra/configure-setup/creating-custom-workers.html
#   https://pages.nist.gov/dioptra/how-to/setup-dioptra/configure-setup/integrating-custom-containers.html
#
# Usage:
#   scripts/e2e-dioptra.sh <deployment-dir>
#
# The deployment must have been created with the official template (for
# example by scripts/setup-dioptra.sh) with `datasets_directory` set: the
# sample manifests are copied there and the ATR worker mounts it read-only at
# /dioptra/data, as the stock workers do.
#
# Environment:
#   DIOPTRA_USERNAME / DIOPTRA_PASSWORD  account used for the API calls; created
#                                        if it does not exist (test deployments)
#   DIOPTRA_URL       API base URL (default: http://localhost)
#   ATR_WORKER_IMAGE  image tag to build (default: atr-dioptra-worker:<container_tag>)
#   ATR_QUEUE         queue the ATR worker serves (default: atr-scanner)
#   PYTHON            python with `requests` installed (default: python3)
set -euo pipefail

DEPLOY_DIR="$(cd "${1:?usage: e2e-dioptra.sh <deployment-dir>}" && pwd)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
ATR_QUEUE="${ATR_QUEUE:-atr-scanner}"

context() {
  "${PYTHON}" -c 'import json, sys; print(json.load(open(sys.argv[1]))["context"]["cookiecutter"][sys.argv[2]])' \
    "${DEPLOY_DIR}/.cruft.json" "$1"
}

SLUG="$(context __project_slug)"
REGISTRY="$(context container_registry)"
TAG="$(context container_tag)"
DATA_DIR="$(context datasets_directory)"
WORKER_BASE="${REGISTRY:+${REGISTRY}/}dioptra/pytorch-cpu:${TAG}"
ATR_WORKER_IMAGE="${ATR_WORKER_IMAGE:-atr-dioptra-worker:${TAG}}"

if [[ -z "${DATA_DIR}" ]]; then
  echo "error: the deployment has no datasets_directory; the ATR worker needs a data mount" >&2
  exit 1
fi
if [[ "${DATA_DIR}" == *'"'* || "${DATA_DIR}" == *$'\n'* ]]; then
  echo "error: datasets_directory contains a quote or newline: ${DATA_DIR}" >&2
  exit 1
fi
# The job imports the committed HEAD while the image is built from the working
# tree: refuse to mix the two.
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain)" ]]; then
  echo "error: uncommitted changes in ${REPO_ROOT}; commit them first (the job imports HEAD)" >&2
  exit 1
fi

echo "== building ${ATR_WORKER_IMAGE} FROM ${WORKER_BASE}"
docker build --build-arg "DIOPTRA_WORKER_BASE=${WORKER_BASE}" -t "${ATR_WORKER_IMAGE}" "${REPO_ROOT}"

echo "== staging sample manifests in ${DATA_DIR}/atr-manifests"
mkdir -p "${DATA_DIR}/atr-manifests"
cp "${REPO_ROOT}"/examples/sample_manifests/* "${DATA_DIR}/atr-manifests/"

OVERRIDE="${DEPLOY_DIR}/docker-compose.override.yml"
SERVICE="${SLUG}-atr-worker"
if [[ -f "${OVERRIDE}" ]] && ! grep -q "${SERVICE}:" "${OVERRIDE}"; then
  echo "error: ${OVERRIDE} exists; add the ${SERVICE} service from README by hand" >&2
  exit 1
fi
if [[ ! -f "${OVERRIDE}" ]]; then
  echo "== writing ${OVERRIDE}"
  cat > "${OVERRIDE}" <<EOF
# Adds the ATR scanner worker (written by atr-dioptra-plugin scripts/e2e-dioptra.sh).
services:
  ${SERVICE}:
    image: ${ATR_WORKER_IMAGE}
    restart: always
    hostname: ${SERVICE}
    healthcheck:
      test: ["CMD", "/usr/local/bin/healthcheck.sh"]
      interval: 30s
      timeout: 60s
      retries: 5
      start_period: 80s
    environment:
      AWS_ACCESS_KEY_ID: \${WORKER_AWS_ACCESS_KEY_ID}
      AWS_SECRET_ACCESS_KEY: \${WORKER_AWS_SECRET_ACCESS_KEY}
      DIOPTRA_WORKER_USERNAME: \${DIOPTRA_WORKER_USERNAME}
      DIOPTRA_WORKER_PASSWORD: \${DIOPTRA_WORKER_PASSWORD}
    command:
      - --wait-for
      - ${SLUG}-redis:6379
      - --wait-for
      - ${SLUG}-minio:9001
      - --wait-for
      - ${SLUG}-db:5432
      - --wait-for
      - ${SLUG}-mlflow-tracking:5000
      - --wait-for
      - ${SLUG}-restapi:5000
      - ${ATR_QUEUE}
    env_file:
      - ./envs/ca-certificates.env
      - ./envs/${SLUG}-worker.env
      - ./envs/${SLUG}-worker-cpu.env
    networks:
      - dioptra
    volumes:
      - "worker-ca-certificates:/usr/local/share/ca-certificates:rw"
      - "worker-etc-ssl:/etc/ssl:rw"
      - "${DATA_DIR}:/dioptra/data:ro"
EOF
fi

echo "== starting ${SERVICE}"
(cd "${DEPLOY_DIR}" && docker compose up -d "${SERVICE}")

echo "== waiting for the REST API"
api_answers() {
  # Unauthenticated requests get 401: any non-5xx answer means the API is up.
  local code
  code="$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' "${DIOPTRA_URL:-http://localhost}/api/v1/groups/")"
  [[ "${code}" != "000" && "${code}" -lt 500 ]]
}
for attempt in $(seq 1 60); do
  api_answers && break
  if [[ "${attempt}" -eq 60 ]]; then
    echo "error: the Dioptra REST API did not come up" >&2
    exit 1
  fi
  sleep 5
done

# build/ is git-ignored; the archive is overwritten on every run.
ARCHIVE="${REPO_ROOT}/build/e2e/atr-dioptra-plugin.tar.gz"
mkdir -p "$(dirname "${ARCHIVE}")"
echo "== archiving HEAD ($(git -C "${REPO_ROOT}" rev-parse --short HEAD)) for upload"
git -C "${REPO_ROOT}" archive --format=tar.gz -o "${ARCHIVE}" HEAD

echo "== running the end-to-end job"
"${PYTHON}" "${REPO_ROOT}/scripts/dioptra_e2e.py" \
  --archive "${ARCHIVE}" \
  --queue "${ATR_QUEUE}" \
  --manifest-location /dioptra/data/atr-manifests \
  --register \
  --expect-files-scanned 2 \
  --expect-files-with-findings 1
