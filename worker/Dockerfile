# Repository worker image. The factory base image carries the harnesses and
# Node; this layer adds the toolchain the SiL gates call and bakes the curated
# worker skills so they are pinned by this image digest.

ARG FACTORY_BASE_IMAGE=ghcr.io/stevie1704/sw-factory-base:v1
ARG UV_VERSION=0.10.9

# uv ships as a static binary, so the version is pinned by an image tag rather
# than by an installer script that resolves a release at build time.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-toolchain

FROM ${FACTORY_BASE_IMAGE}

ARG UV_VERSION=0.10.9

USER root

LABEL org.opencontainers.image.title="SiL framework worker" \
      org.opencontainers.image.version="1" \
      org.opencontainers.image.description="Software Factory worker with the SiL C++20 and Python toolchain"

# cmake and build-essential build the kernel. python3 is separate from the
# venv the gates use: the CMake build calls a bare `python3` to generate the
# typed message headers (tools/silschema.py), which imports only the standard
# library. Debian bookworm supplies cmake 3.25 (the build needs 3.24) and
# gcc 12 (the kernel is C++20).
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      python3 \
    && rm -rf /var/lib/apt/lists/*

# The worker's fixed PATH already contains /usr/local/bin, so the binary is
# reachable without a profile or a PATH mutation.
COPY --from=uv-toolchain /uv /usr/local/bin/uv

# Both harnesses read a skill set from their own role home. Baking the set pins
# the agent-visible instructions to this image digest.
COPY --chown=factory:factory skills /home/factory/.codex/skills
COPY --chown=factory:factory skills /home/factory/.claude/skills

USER factory
