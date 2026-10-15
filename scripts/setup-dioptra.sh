#!/usr/bin/env bash
# Create and start a local Dioptra deployment with the official cruft template.
#
# Follows https://pages.nist.gov/dioptra/how-to/setup-dioptra/prepare-deployment.html
# (Method 3: defaults plus a few overrides). Used by CI and for local checks.
#
# One deviation, needed for any fresh deployment as of 2026-10-16: the template
# references minio/minio and minio/mc, which are no longer published (Docker
# Hub answers 404, dl.min.io 410). Stand-in images are built from
# ci/minio/Dockerfile and substituted for those two references only; every
# Dioptra image and all other services come from the template unchanged. Set
# USE_MINIO_SHIM=0 to use the template's references as they are.
#
# Usage:
#   scripts/setup-dioptra.sh <deployments-parent-dir> [data-dir]
#
# Environment:
#   DIOPTRA_REF         template branch/tag to apply (default: main = latest release)
#   DIOPTRA_TEMPLATE    template source (default: https://github.com/usnistgov/dioptra)
#   DEPLOYMENT_NAME     deployment name; its slug prefixes every service (default: atr-e2e)
#   PYTHON              python >= 3.11 with cruft installed (default: python3)
#   USE_MINIO_SHIM      1 (default) or 0, see above
set -euo pipefail

PARENT_DIR="${1:?usage: setup-dioptra.sh <deployments-parent-dir> [data-dir]}"
DATA_DIR="${2:-${PARENT_DIR}/data}"
DIOPTRA_REF="${DIOPTRA_REF:-main}"
DIOPTRA_TEMPLATE="${DIOPTRA_TEMPLATE:-https://github.com/usnistgov/dioptra}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-atr-e2e}"
PYTHON="${PYTHON:-python3}"
USE_MINIO_SHIM="${USE_MINIO_SHIM:-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "${PARENT_DIR}" "${DATA_DIR}"
PARENT_DIR="$(cd "${PARENT_DIR}" && pwd)"
DATA_DIR="$(cd "${DATA_DIR}" && pwd)"

if [[ "${USE_MINIO_SHIM}" == "1" ]]; then
  echo "== building MinIO stand-in images (see ci/minio/Dockerfile)"
  docker build --target minio -t minio-shim/minio:latest "${REPO_ROOT}/ci/minio"
  docker build --target mc -t minio-shim/mc:latest "${REPO_ROOT}/ci/minio"
fi

# Kept in a function: bash 3.2 (macOS) misparses a heredoc inside $(...).
extra_context() {
  "${PYTHON}" - "$@" <<'EOF'
import json, sys
name, data_dir, use_shim = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
context = {
    "deployment_name": name,
    "nginx_server_name": "_",
    "nginx_expose_ports_on_localhost_only": "True",
    # The ATR worker is added by e2e-dioptra.sh; one stock worker is kept as
    # a baseline that the deployment itself works.
    "num_tensorflow_cpu_workers": "0",
    "num_pytorch_cpu_workers": "1",
    "datasets_directory": data_dir,
}
if use_shim:
    # Merged into the private __containers map of the template: only these two
    # image references change.
    context["__containers"] = {
        "minio": {"image": "minio", "namespace": "minio-shim", "tag": "latest", "registry": ""},
        "mc": {"image": "mc", "namespace": "minio-shim", "tag": "latest", "registry": ""},
    }
print(json.dumps(context))
EOF
}
EXTRA_CONTEXT="$(extra_context "${DEPLOYMENT_NAME}" "${DATA_DIR}" "${USE_MINIO_SHIM}")"

cd "${PARENT_DIR}"
"${PYTHON}" -m cruft create "${DIOPTRA_TEMPLATE}" \
  --checkout "${DIOPTRA_REF}" \
  --directory cookiecutter-templates/cookiecutter-dioptra-deployment \
  --no-input --extra-context "${EXTRA_CONTEXT}"

SLUG="$("${PYTHON}" -c "import json,sys; print(json.loads(sys.argv[1])['deployment_name'].lower().replace(' ', '-').replace('_', '-'))" "${EXTRA_CONTEXT}")"
cd "${PARENT_DIR}/${SLUG}"

# pytorchcpu: bootstrap the worker SSL volumes from the image the ATR worker is
# built on, so the tensorflow image is not needed at all.
./init-deployment.sh --branch "${DIOPTRA_REF}" --worker-ssl-service pytorchcpu \
  --python "$(command -v "${PYTHON}")"
docker compose up -d

echo "deployment ready: ${PARENT_DIR}/${SLUG}"
