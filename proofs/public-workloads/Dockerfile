# syntax=docker/dockerfile:1
# The public-workload qualification tools (issue #178): compiler, exporter,
# independent importer and log decoder. Only this build uses the network; it
# fetches every pinned public source and fails unless each digest matches.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY proofs/public-workloads/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY proofs/public-workloads/fetch.py proofs/public-workloads/sources.json /tmp/fetch/
RUN python /tmp/fetch/fetch.py /sources
WORKDIR /src
COPY . .
ARG SOURCE_REVISION
RUN test -n "$SOURCE_REVISION" && echo "$SOURCE_REVISION" > source-revision.txt
ENV PYTHONDONTWRITEBYTECODE=1
# The repository-authored ACC FMUs, exported exactly as proofs/acc-fmi does.
RUN python proofs/acc-fmi/build.py /fmus
WORKDIR /work
CMD ["python", "/src/proofs/public-workloads/prepare.py", "/work"]
