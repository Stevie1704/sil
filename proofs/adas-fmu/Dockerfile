# syntax=docker/dockerfile:1
# The ADAS reference FMU preparation environment (issue #225): the pinned
# compiler that builds the archive and the pinned FMPy that checks it. Only
# this image build uses the network. The Run-time example image never needs
# FMPy or this compiler: it takes the qualified archive.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
# The Debian snapshot pins the compiler and linker; the archive identity
# records `cc --version`.
COPY container/install-snapshot-packages.sh /tmp/install-snapshot-packages.sh
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && sh /tmp/install-snapshot-packages.sh build-essential \
    && rm -f /tmp/install-snapshot-packages.sh \
    && rm -rf /var/lib/apt/lists/*
# The existing pinned FMPy preparation path: FMPy 0.3.26 by hash.
COPY proofs/acc-fmi/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
WORKDIR /src
COPY . .
ENV PYTHONPATH=/src/python/src PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
CMD ["python", "/src/proofs/adas-fmu/prove.py", "/work"]
