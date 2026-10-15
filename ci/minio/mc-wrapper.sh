#!/usr/bin/env bash
# `mc` for the minio-shim/mc stand-in image.
#
# The deployment template's init-deployment.sh starts the MinIO container and
# immediately runs init-minio.sh, whose first command is `mc alias set`; it
# does not wait for MinIO to listen. When the client starts first, the alias
# fails with "connect: connection refused" and the whole init aborts (a known
# race in the template, seen on fast CI runners). Before an `alias set`, this
# wrapper waits until the target endpoint passes MinIO's liveness probe, then
# runs the real client unchanged.
set -euo pipefail

readonly REAL_MC=/usr/local/bin/mc-real
readonly WAIT_SECONDS="${MC_WAIT_FOR_ENDPOINT_SECONDS:-60}"

if [[ "${1:-}" == "alias" && "${2:-}" == "set" && -n "${4:-}" ]]; then
  endpoint="${4%/}"
  for ((elapsed = 0; elapsed < WAIT_SECONDS; elapsed++)); do
    if curl -fs -o /dev/null --max-time 2 "${endpoint}/minio/health/live"; then
      break
    fi
    sleep 1
  done
fi

exec -a mc "${REAL_MC}" "$@"
