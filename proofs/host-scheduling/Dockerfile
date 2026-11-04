ARG UBUNTU_IMAGE=ubuntu:24.04
FROM ${UBUNTU_IMAGE}
ARG PERF_PACKAGE
RUN apt-get update && apt-get install -y --no-install-recommends \
    "$PERF_PACKAGE" linux-tools-common python3 gcc libc6-dev binutils libssl3t64 \
    && rm -rf /var/lib/apt/lists/*
