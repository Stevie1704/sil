# syntax=docker/dockerfile:1

# The libsafety adoption example (issue #193): the production SiL runtime
# image plus the one runtime dependency the library brings and the consumer
# material that binds, runs and judges it. No compiler, no opendbc, no CFFI
# and no log decoder enter this image: the library comes pinned from the #178
# bundle and the frames come exported by the tool image.
ARG SIL_RUNTIME_IMAGE
FROM ${SIL_RUNTIME_IMAGE}

USER root

# The library is built with UBSan, as upstream builds it, so it links
# libubsan.so.1. Debian's package index is frozen like the runtime image's.
ARG DEBIAN_SNAPSHOT=20260901T000000Z
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends libubsan1 \
    && rm -rf /var/lib/apt/lists/*

# The #178 handoff as committed: the digests the acceptance pins.
COPY proofs/public-workloads/evidence/handoff.json /opt/libsafety/handoff.json
COPY proofs/libsafety/acceptance.py \
     proofs/libsafety/adapter.py \
     proofs/libsafety/binding.py \
     proofs/libsafety/library_failures.py \
     proofs/libsafety/manifest.py \
     proofs/libsafety/workload.py \
     /opt/libsafety/

RUN install -d -o 10001 -g 10001 /bundle /prepared

USER 10001:10001
WORKDIR /workspace
