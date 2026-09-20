# syntax=docker/dockerfile:1
#
# The derived consumer image for the esmini adoption proof (issue #117).
#
# It starts from the published SiL runner image *by digest* and adds only
# consumer-side material: the upstream esmini binary drop, the scenario and
# road files, the shared libraries esmini needs, and this consumer's two
# Participants. Nothing is copied from a SiL checkout or build directory, and
# the base image is not modified in place.

# The fetch stage is a plain Debian image, not the SiL image: downloading and
# unpacking a third-party archive is consumer work and has no business running
# in a layer derived from the runner.
FROM debian:bookworm-slim AS fetch

ARG ESMINI_VERSION=v3.8.1
ARG ESMINI_BIN_SHA256=05e6c9bb0f3156cef6f193c126b5ae340d42256da9c4a9eb7436d3b99dee9654
ARG ESMINI_SRC_SHA256=dacb0d3c7e8919e27c456d5cbbc3458c07134fa6ce3f84800dc1f0473d77105e

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /fetch
RUN base="https://github.com/esmini/esmini/releases/download/${ESMINI_VERSION}" \
    && curl -fsSL -o bin.zip "${base}/esmini-bin_Linux.zip" \
    && curl -fsSL -o src.zip "${base}/esmini-fullsrc.zip" \
    && echo "${ESMINI_BIN_SHA256}  bin.zip" | sha256sum -c - \
    && echo "${ESMINI_SRC_SHA256}  src.zip" | sha256sum -c - \
    && unzip -q bin.zip 'esmini/bin/libesminiLib.so' 'esmini/version.txt' \
      'esmini/EnvironmentSimulator/Libraries/esminiLib/esminiLib.hpp' \
    && unzip -q src.zip 'esmini/resources/xosc/*' 'esmini/resources/xodr/*' \
      'esmini/test/smoke_test.py' 'esmini/LICENSE' \
    && install -d /opt/esmini \
    && mv esmini/bin /opt/esmini/bin \
    && mv esmini/resources /opt/esmini/resources \
    && mv esmini/test /opt/esmini/test \
    && mv esmini/version.txt /opt/esmini/version.txt \
    && mv esmini/LICENSE /opt/esmini/LICENSE \
    && mv esmini/EnvironmentSimulator/Libraries/esminiLib/esminiLib.hpp \
      /opt/esmini/esminiLib.hpp

# The published SiL runner image, pinned by content digest (issue #117 AC 4).
# The 0.1.0 tag would resolve to whatever that tag points at today; the digest
# is the artifact this proof was executed against.
FROM ghcr.io/stevie1704/sil@sha256:d67a254125fbe44467bf1354a04d19955815098d86a4c94c74f73ead6a943c0b AS consumer

USER root

# libesminiLib.so links the viewer's OpenSceneGraph dependencies even when the
# Run never instantiates a viewer, so the loader needs them present before the
# library can be dlopen'd at all. These are consumer runtime dependencies of
# the consumer artifact; see README.md, "What required consumer-side work".
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libfontconfig1 \
      libgl1 \
      libx11-6 \
      libxinerama1 \
      libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /opt/esmini /opt/esmini
COPY participants /opt/consumer/participants
COPY schemas /opt/consumer/schemas
COPY reference /opt/consumer/reference
COPY manifest.py verify.py observe.py clock_shim_control.py /opt/consumer/

ENV ESMINI_LIBRARY=/opt/esmini/bin/libesminiLib.so \
    ESMINI_RESOURCES=/opt/esmini/resources

USER 10001:10001
