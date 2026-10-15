# Stand-ins for the minio/minio and minio/mc images that Dioptra's deployment
# template references (1.2.1, and dev as of 2026-10-16).
#
# Both Docker Hub repositories now answer 404 and dl.min.io answers 410 for the
# community binaries, so a fresh deployment from the template cannot pull its
# object store. These images take the minio and mc binaries from Chainguard's
# source builds and put them on debian-slim, which provides the bash, curl and
# coreutils that the template's healthcheck and init scripts call. Entrypoints
# match the original images, so the template's commands work unchanged.
#
#   docker build --target minio -t minio-shim/minio:latest ci/minio
#   docker build --target mc    -t minio-shim/mc:latest    ci/minio
ARG MINIO_SOURCE=cgr.dev/chainguard/minio:latest
ARG MC_SOURCE=cgr.dev/chainguard/minio-client:latest
ARG BASE=debian:bookworm-slim

FROM ${MINIO_SOURCE} AS minio-src
FROM ${MC_SOURCE} AS mc-src

FROM ${BASE} AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

FROM base AS minio
COPY --from=minio-src /usr/bin/minio /usr/bin/minio
ENTRYPOINT ["minio"]

FROM base AS mc
COPY --from=mc-src /usr/bin/mc /usr/bin/mc
ENTRYPOINT ["mc"]
