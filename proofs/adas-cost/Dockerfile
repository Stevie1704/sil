# syntax=docker/dockerfile:1
# The reference execution-cost measurement of the ADAS controller (issue
# #229): installed SiL with the instrumented runner beside the production
# one, and the pinned compiler of the #226 proof. Only this image build uses
# the network.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
# The Debian snapshot of proofs/adas-fmu and proofs/adas-equivalence: the
# same compiler builds the library, the archive and the timing harness.
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Installed SiL, as a consumer installs it: the native prefix (sil-run,
# silschema, include/sil) and the wheel in its own environment. The
# instrumented runner is the same kernel sources with copy counters; it is
# installed beside sil-run so it finds the same clock shim. The measurement
# runs with no checkout on any import path.
COPY CMakeLists.txt LICENSE NOTICE THIRD-PARTY-NOTICES.md ./
COPY include include
COPY kernel kernel
COPY shim shim
COPY tests tests
COPY tools tools
COPY cmake cmake
COPY python python
COPY container container
RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build --target sil-run sil-run-instrumented sil_clock_shim -j2 \
    && cmake --install /build --prefix /opt/sil/native \
    && install -m 0755 /build/sil-run-instrumented /opt/sil/native/bin/ \
    && python tools/release.py stamp-python --root /src --output-dir /release-python \
      --version "$(python tools/release.py project-version --root /src)" \
      --source-revision checkout \
    && python -m pip install --no-cache-dir --requirement container/build-requirements.lock \
    && python -m pip wheel --no-build-isolation --no-deps --wheel-dir /wheels /release-python \
    && python -m pip download --only-binary=:all: --dest /wheels \
      --requirement container/requirements.lock \
    && python -m venv /opt/sil/python \
    && /opt/sil/python/bin/pip install --no-cache-dir --no-index --find-links=/wheels \
      --requirement container/requirements.lock /wheels/sil-*.whl
# The application, its FMU sources, the adapter and the measurement.
COPY . .
ENV PATH=/opt/sil/python/bin:/opt/sil/native/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
CMD ["python", "/src/proofs/adas-cost/measure.py", "/work"]
