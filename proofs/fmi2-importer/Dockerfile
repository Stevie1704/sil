# syntax=docker/dockerfile:1
# The FMI 2.0 importer acceptance (issue #191): the kernel and Importer of
# this checkout, FMPy 0.3.26 as the independent FMI 2.0 importer, and the
# OSMP toolchain that builds OSMPDummySensor exactly as proofs/osmp-sensor
# does. Only this build uses the network: it fetches the pinned OSMP checkout
# and fails unless commit, tree and submodules match. The Reference FMUs are
# the archives vendored under tests/fixtures/reference-fmus/2.0/.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake ca-certificates git \
        libprotobuf-dev protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
# FMPy 0.3.26 and the OSMP build tools, pinned by hash, and the SiL runtime
# closure (mcap), pinned by exact version as the runtime image installs it.
COPY proofs/osmp-sensor/requirements.lock /tmp/requirements.lock
COPY container/requirements.lock /tmp/runtime.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && pip install --no-cache-dir --only-binary=:all: -r /tmp/runtime.lock
COPY proofs/osmp-sensor/fetch.py proofs/osmp-sensor/sources.json /tmp/fetch/
RUN python /tmp/fetch/fetch.py /sources
WORKDIR /src
COPY . .
ARG SOURCE_REVISION
RUN test -n "$SOURCE_REVISION" && echo "$SOURCE_REVISION" > source-revision.txt
ENV PYTHONPATH=/src/python/src PYTHONDONTWRITEBYTECODE=1
# The kernel, and the fake FMI 2.0 FMU whose fmi2DoStep answers Error.
RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE=Release -DSIL_SOURCE_REVISION="$SOURCE_REVISION" \
    && cmake --build /build --target sil-run sil_clock_shim fmi2_fake_StepError -j2
WORKDIR /work
CMD ["python", "/src/proofs/fmi2-importer/qualify.py", "/work"]
