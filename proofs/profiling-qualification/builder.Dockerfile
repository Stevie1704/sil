# syntax=docker/dockerfile:1
# Builds the qualification workload (#319) with the compiler and libc of the
# runtime image's base, Debian bookworm, so the binaries run in the profiling
# image. It is not the qualified environment: the profiling image has no
# compiler. The checkout is mounted at run time; its Python package is used
# from there with the pinned dependencies of the image.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
ARG DEBIAN_SNAPSHOT=20260901T000000Z
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential binutils \
    && rm -rf /var/lib/apt/lists/*
COPY container/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --requirement /tmp/requirements.lock
ENV PYTHONPATH=/src/python/src PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
