# syntax=docker/dockerfile:1

# The tag documents the Python release; the digest makes every build resolve
# the same multi-platform OCI index. It contains both amd64 (CI) and arm64.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d

FROM ${PYTHON_IMAGE} AS build

# Freeze Debian's package index as well as the base filesystem. Build tools
# never cross into the runtime stage.
ARG DEBIAN_SNAPSHOT=20260901T000000Z
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && printf '%s\n' \
      "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm-updates main" \
      "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} bookworm-security main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY CMakeLists.txt ./
COPY include include
COPY kernel kernel
COPY participants participants
COPY schemas schemas
COPY shim shim
COPY tests tests
COPY tools tools
COPY python python
COPY container container

RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build --target sil-run sil_clock_shim -j2 \
    && cmake --install /build --prefix /opt/sil/native

RUN python -m pip install --no-cache-dir \
      --requirement container/build-requirements.lock \
    && python -m pip wheel --no-build-isolation --no-deps \
      --wheel-dir /wheels ./python \
    && python -m pip download --only-binary=:all: --dest /wheels \
      --requirement container/requirements.lock \
    && python -m venv /opt/sil/python \
    && /opt/sil/python/bin/pip install --no-cache-dir --no-index \
      --find-links=/wheels \
      --requirement container/requirements.lock \
      /wheels/sil-*.whl

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="SiL Run runtime" \
      org.opencontainers.image.description="One deterministic SiL Run per Linux container" \
      org.opencontainers.image.source="https://github.com/Stevie1704/sil"

COPY --from=build /opt/sil/native/bin/sil-run /usr/local/bin/sil-run
COPY --from=build /opt/sil/native/lib/libsil_clock_shim.so /usr/local/lib/libsil_clock_shim.so
COPY --from=build /opt/sil/python /opt/sil/python
COPY --from=build /build/_deps/mcap-src/LICENSE /usr/share/licenses/sil/mcap/LICENSE
COPY --from=build /build/_deps/nlohmann_json-src/LICENSE.MIT /usr/share/licenses/sil/nlohmann-json/LICENSE.MIT

ENV HOME=/tmp \
    PATH="/opt/sil/python/bin:/usr/local/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 sil \
    && useradd --uid 10001 --gid sil --no-create-home --home-dir /tmp sil \
    && install -d -o sil -g sil /workspace

WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["sil-run"]

# Reference-only assets deliberately live in a derived target. The production
# runtime above exposes no test fixture or FMU supplied by this repository.
FROM runtime AS acceptance

USER root
COPY --chown=10001:10001 examples/fmu /opt/sil/reference/examples/fmu
COPY --chown=10001:10001 schemas/fmu.json /opt/sil/reference/schemas/fmu.json
COPY --chown=10001:10001 tests/fixtures/reference-fmus/3.0/Feedthrough.fmu /opt/sil/reference/tests/fixtures/reference-fmus/3.0/Feedthrough.fmu
COPY --chown=10001:10001 tests/fixtures/reference-fmus/LICENSE.txt /opt/sil/reference/tests/fixtures/reference-fmus/LICENSE.txt
USER 10001:10001
