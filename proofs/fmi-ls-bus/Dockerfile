# syntax=docker/dockerfile:1
#
# The two images the FMI-LS-BUS CAN acceptance fixture needs (issue #137).
#
# `fixture` builds the pinned upstream CAN demo FMUs and carries the
# independent FMI 3.0 importer that executes them. It is deliberately not
# derived from any SiL image: a reference exchange that shared a process, a
# library, or a Python environment with the importer it is evidence for would
# be worth less than one that does not.
#
# `proof` starts from the published SiL runner image *by digest* and adds only
# the fixture FMUs and the consumer material that puts them in front of the
# released FMI Importer. Nothing is copied from a SiL checkout or build
# directory.

# Pinned to the linux/amd64 manifest digest of python:3.13.7-slim-bookworm,
# the base the released runner image is built from. The platform is part of
# the pin rather than the host's: the supported machine class is x86-64 Linux,
# and a fixture built for a developer's own architecture would not be the
# fixture CI executes.
FROM python@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7 AS fixture

# The upstream revision this fixture is pinned to: `main`, as merged.
#
# The head of the open `Fix and update CAN FMUs` pull request is built beside
# it, because it is the obvious alternative — it is the only upstream state
# that labels the CAN demos FMI-LS-BUS 1.0.0. It is not selected, and
# README.md records why with the evidence the proof produces: the state check
# it adds to the clocked Binary variable rejects the read order FMI 3.0
# prescribes, so the same reference exchange that passes against `main` fails
# against it.
ARG EXAMPLES_REVISION=cc42cacd26c7f5edbb20959b0e0c56922c2f0cc2
ARG EXAMPLES_ALTERNATIVE_REVISION=de019a6efbad810795835f2fd9bbf9e62eb451b9
# FMPy supplies the FMI 3.0 bindings the reference exchange drives the FMU
# through, and the source-code FMU compiler the fixture build calls.
ARG FMPY_VERSION=0.3.32

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
      git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "fmpy==${FMPY_VERSION}"

COPY build-fixture.sh pack_fixture.py /usr/local/bin/

# The built FMUs are what says the build worked: a pipe into `tee` reports the
# exit status of `tee`, so the artifacts are checked rather than the status.
RUN build-fixture.sh "${EXAMPLES_REVISION}" /opt/fixture \
      2>&1 | tee /opt/fixture-build.log \
    && test -s /opt/fixture/DemoCanNodeTriggeredOutput.fmu \
    && test -s /opt/fixture/DemoCanBusSimulation.fmu \
    && rm -rf /fixture-build

# The same revision again, into a second directory, and the two are compared
# byte for byte. Pinning a built artifact's digest is only worth doing if the
# pinned image produces the same artifact twice: the archives are packed with
# a fixed timestamp, so what this compares is the compiler's output.
RUN build-fixture.sh "${EXAMPLES_REVISION}" /opt/fixture-repeat \
      > /opt/repeat-build.log 2>&1 \
    && for fmu in /opt/fixture/*.fmu; do \
         cmp "$fmu" "/opt/fixture-repeat/$(basename "$fmu")" \
           && echo "$(basename "$fmu") is byte-identical to the first build" \
              >> /opt/repeat-build.log; \
       done \
    && rm -rf /opt/fixture-repeat /fixture-build

# The alternative revision, built the same way and kept beside the fixture so
# the reference exchange can be run against both. Its build succeeds; what
# rules it out is what it does when it is driven.
RUN build-fixture.sh "${EXAMPLES_ALTERNATIVE_REVISION}" /opt/alternative \
      2>&1 | tee /opt/alternative-build.log \
    && test -s /opt/alternative/DemoCanNodeTriggeredOutput.fmu \
    && rm -rf /fixture-build

COPY can_operations.py expected.json inspect_fixture.py \
     reference_exchange.py /opt/harness/

WORKDIR /opt/harness

# The published SiL runner image, pinned by content digest: the release the
# fixture is evidence about, rather than whatever the 0.1.0 tag resolves to
# today.
FROM ghcr.io/stevie1704/sil@sha256:d67a254125fbe44467bf1354a04d19955815098d86a4c94c74f73ead6a943c0b AS proof

USER root

COPY --from=fixture /opt/fixture/*.fmu /opt/fixture/
COPY manifest.py observer.py /opt/consumer/

USER 10001:10001
