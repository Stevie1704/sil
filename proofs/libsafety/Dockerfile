# syntax=docker/dockerfile:1

# The libsafety adoption example (issues #193, #232): the production SiL
# runtime image plus the one runtime dependency the library brings and the
# consumer material that binds, runs and judges it. No compiler, no opendbc,
# no CFFI and no log decoder enter this image: the library comes pinned from
# the #178 bundle and the frames come exported by the tool image.
ARG SIL_BUILD_IMAGE
ARG SIL_RUNTIME_IMAGE

# The Native adapter (#232), built in the SiL build stage against the
# installed ABI header and the layouts silschema generates from workload.py.
# The crash and hang controls are the same source with one failure chosen by
# the preprocessor.
FROM ${SIL_BUILD_IMAGE} AS native
WORKDIR /native
COPY proofs/libsafety/native_adapter.c proofs/libsafety/workload.py ./
RUN python -c "import json, workload as w; print(json.dumps({k: w.SCHEMAS[k] for k in (w.TIMED_FRAME_SCHEMA, w.NATIVE_STATE_SCHEMA)}))" > schemas.json \
    && /opt/sil/native/bin/silschema schemas.json libsafety_messages.h \
    && build() { cc -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra -Werror -pedantic \
         -fPIC -shared -I/opt/sil/native/include -I. "$@" native_adapter.c; } \
    && mkdir out \
    && build -o out/libsafety_native.so \
    && build -DLIBSAFETY_FAILURE_AT_EVENT=100 -DLIBSAFETY_FAILURE_CRASH -o out/libsafety_native_crash.so \
    && build -DLIBSAFETY_FAILURE_AT_EVENT=100 -DLIBSAFETY_FAILURE_HANG -o out/libsafety_native_hang.so

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
     proofs/libsafety/native_acceptance.py \
     proofs/libsafety/workload.py \
     /opt/libsafety/
COPY --from=native /native/out/ /opt/libsafety/native/

RUN install -d -o 10001 -g 10001 /bundle /prepared

USER 10001:10001
WORKDIR /workspace
