# syntax=docker/dockerfile:1
# The OSMP FMU qualification (issue #233). The `prepare` stage holds the
# checkout, the compiler, protoc and the pinned OSMP sources: it builds both
# FMUs as proofs/osmp-sensor does, inspects them, writes the independent
# references and the bundles. The `runtime` stage is a fresh interpreter with
# installed SiL, the sealed bundles and the libraries the FMUs load: no
# checkout, compiler, protoc, exporter or FMPy. Only the image build uses
# the network.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
ARG DEBIAN_SNAPSHOT=http://snapshot.debian.org/archive/debian/20260901T000000Z

FROM ${PYTHON_IMAGE} AS prepare
ARG DEBIAN_SNAPSHOT
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo "deb [check-valid-until=no] ${DEBIAN_SNAPSHOT} bookworm main" > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake ca-certificates git \
        libprotobuf-dev protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
# The OSMP build tools and Python Protobuf, pinned by hash, and the SiL
# runtime closure for the preparation interpreter.
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
# Installed SiL, as a consumer installs it: the native prefix and the wheel
# in its own environment (the recipe of proofs/adas-cost).
RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE=Release -DSIL_SOURCE_REVISION="$SOURCE_REVISION" \
    && cmake --build /build --target sil-run sil_clock_shim -j2 \
    && cmake --install /build --prefix /opt/sil/native \
    && python tools/release.py stamp-python --root /src --output-dir /release-python \
      --version "$(python tools/release.py project-version --root /src)" \
      --source-revision "$SOURCE_REVISION" \
    && python -m pip install --no-cache-dir --requirement container/build-requirements.lock \
    && python -m pip wheel --no-build-isolation --no-deps --wheel-dir /wheels /release-python \
    && python -m pip download --only-binary=:all: --dest /wheels \
      --requirement container/requirements.lock \
    && python -m venv /opt/sil/python \
    && /opt/sil/python/bin/pip install --no-cache-dir --no-index --find-links=/wheels \
      --requirement container/requirements.lock /wheels/sil-*.whl
ENV PYTHONPATH=/src/python/src PYTHONDONTWRITEBYTECODE=1
RUN python proofs/osmp-qualification/prepare.py /prepared

FROM ${PYTHON_IMAGE} AS runtime
ARG DEBIAN_SNAPSHOT
# The one system library the FMUs load beyond the C and C++ runtimes, from
# the snapshot they were built against.
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo "deb [check-valid-until=no] ${DEBIAN_SNAPSHOT} bookworm main" > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends libprotobuf32 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=prepare /opt/sil/native /opt/sil/native
COPY --from=prepare /opt/sil/python /opt/sil/python
COPY proofs/osmp-qualification/requirements.lock /tmp/requirements.lock
RUN /opt/sil/python/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY --from=prepare /bundles /bundles
COPY --from=prepare /prepared/expected.json /opt/osmp/expected.json
COPY --from=prepare /prepared/cost /opt/osmp/cost
COPY proofs/osmp-qualification/run.py /opt/osmp/run.py
ENV PATH=/opt/sil/python/bin:/opt/sil/native/bin \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python /opt/osmp/run.py --seal
RUN ["/bin/sh", "-c", "/bin/mkdir -p /work && /bin/chown -R 10001:10001 /work"]
WORKDIR /work
USER 10001:10001
CMD ["python", "/opt/osmp/run.py"]
