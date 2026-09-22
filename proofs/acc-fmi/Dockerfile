# syntax=docker/dockerfile:1
FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
# PythonFMU3 embeds Python source, not a Python runtime. This image supplies
# CPython and libpython3.13.so, plus all declared Python dependencies.
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends build-essential cmake ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY proofs/acc-fmi/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
# Unlike the released-runtime consumer proofs, this is a checkout qualification:
# issue #147 introduces the resourcePath correction being tested. A published
# image cannot prove that unreleased change. SOURCE_REVISION binds this runner
# and the generated FMUs to the exact checkout; the base/runtime remain pinned.
WORKDIR /src
COPY . .
ARG SOURCE_REVISION
RUN test -n "$SOURCE_REVISION" && echo "$SOURCE_REVISION" > source-revision.txt
ENV PYTHONPATH=/src/python/src PYTHONDONTWRITEBYTECODE=1
RUN cmake -S . -B /build -DCMAKE_BUILD_TYPE=Release -DSIL_SOURCE_REVISION="$SOURCE_REVISION" \
    && cmake --build /build --target sil-run sil_clock_shim -j2
RUN python proofs/acc-fmi/build.py /fmus
WORKDIR /work
CMD ["python", "/src/proofs/acc-fmi/qualify.py", "/work"]
