# syntax=docker/dockerfile:1
# The OSMP FMU target tools (issue #230): compiler, CMake, Protobuf and an
# independent FMI 2.0 importer. Only this build uses the network; it fetches
# the pinned OSMP checkout and fails unless commit, tree and submodules match.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake ca-certificates git \
        libprotobuf-dev protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
COPY proofs/osmp-sensor/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY proofs/osmp-sensor/fetch.py proofs/osmp-sensor/sources.json /tmp/fetch/
RUN python /tmp/fetch/fetch.py /sources
WORKDIR /src
COPY proofs/osmp-sensor proofs/osmp-sensor
ARG SOURCE_REVISION
RUN test -n "$SOURCE_REVISION" && echo "$SOURCE_REVISION" > source-revision.txt
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
CMD ["python", "/src/proofs/osmp-sensor/prepare.py", "/work"]
