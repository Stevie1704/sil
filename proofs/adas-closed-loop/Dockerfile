# syntax=docker/dockerfile:1
# The mixed closed loop of issue #227: installed SiL, the pinned compiler
# that reproduces the #225 controller archive, the ACC plant archive built
# by the pinned PythonFMU3 exporter of proofs/acc-fmi, and FMPy as the
# independent FMI execution. Only this image build uses the network.
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${PYTHON_IMAGE}
# The Debian snapshot of proofs/adas-fmu: the same compiler gives the same
# controller archive bytes. procps gives `ps`, for the check that no process
# of a failed Run outlives it.
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake procps \
    && rm -rf /var/lib/apt/lists/*
# PythonFMU3 0.3.4 and FMPy 0.3.26 by hash, in the base interpreter only.
# The SiL environment below has neither, and independent.py imports no SiL.
# The plant archive runs on this interpreter's libpython3.13.so.
COPY proofs/acc-fmi/requirements.lock /tmp/acc-fmi.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/acc-fmi.lock
WORKDIR /src
# Installed SiL, as a consumer installs it: the native prefix and the wheel
# in its own environment, as proofs/adas-equivalence installs them.
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
COPY . .
# The plant archive, from the qualified model and dynamics sources. Its
# identity names this checkout; prove.py holds the model to the identity the
# ACC evidence qualified.
ARG SOURCE_REVISION
RUN test -n "$SOURCE_REVISION" && echo "$SOURCE_REVISION" > source-revision.txt \
    && python proofs/acc-fmi/build.py /fmus
ENV PATH=/opt/sil/python/bin:/opt/sil/native/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
CMD ["python", "/src/proofs/adas-closed-loop/prove.py", "/work", \
     "--plant", "/fmus/AccPlant.fmu", "--fmpy-python", "/usr/local/bin/python"]
