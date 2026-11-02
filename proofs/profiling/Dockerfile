# syntax=docker/dockerfile:1
# The documented container setup of the profiling proof (issue #314):
# installed SiL, the pinned compiler of proofs/adas-cost, and perf and
# Heaptrack from the same Debian snapshot. Only this image build uses the
# network. The proof runs as root in the container; what it may do depends
# on the `docker run` options and the host, and container.sh records both.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
# Debian snapshot: build-essential (GCC 12.2), binutils (objcopy, readelf),
# linux-perf (perf 6.1) and heaptrack (1.4.0). The exact package versions are
# in probe.json and results.json.
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake libssl-dev \
       binutils linux-perf heaptrack \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Installed SiL, as in proofs/adas-cost: the native prefix and the wheel in
# its own environment, with no checkout on any import path.
COPY CMakeLists.txt LICENSE NOTICE THIRD-PARTY-NOTICES.md ./
COPY GLOSSARY.md SUPPORT.md ./
COPY docs docs
COPY examples examples
COPY include include
COPY kernel kernel
COPY shim shim
COPY tests tests
COPY tools tools
COPY cmake cmake
COPY python python
COPY container container
RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build --target sil-run sil-run-diagnostics sil-diagnostics-launcher sil_clock_shim -j2 \
    && cmake --install /build --prefix /opt/sil/native \
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
COPY . .
ENV PATH=/opt/sil/python/bin:/opt/sil/native/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
CMD ["python", "/src/proofs/profiling/proof.py", "run", "/work/evidence"]
