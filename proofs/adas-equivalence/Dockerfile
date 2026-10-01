# syntax=docker/dockerfile:1
# The native/FMU equivalence proof of the ADAS reference controller (issue
# #226): installed SiL, the pinned compiler that reproduces the #225 archive,
# and FMPy as the independent FMI execution. Only this image build uses the
# network.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
# The Debian snapshot of proofs/adas-fmu: the same compiler gives the same
# archive bytes, so the archive digest can be held to its pin. procps gives
# `ps`, for the check that no importer process outlives its Run.
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake procps \
    && rm -rf /var/lib/apt/lists/*
# FMPy 0.3.26 by hash, in the base interpreter only. The SiL environment
# below has no FMPy, and independent.py imports no SiL.
COPY proofs/acc-fmi/requirements.lock /tmp/fmpy.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/fmpy.lock
WORKDIR /src
COPY . .
# Installed SiL, as a consumer installs it: the native prefix (sil-run,
# silschema, include/sil) and the wheel in its own environment. The proof
# runs with no checkout on any import path.
RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build --target sil-run sil_clock_shim -j2 \
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
ENV PATH=/opt/sil/python/bin:/opt/sil/native/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
CMD ["python", "/src/proofs/adas-equivalence/prove.py", "/work", \
     "--fmpy-python", "/usr/local/bin/python"]
