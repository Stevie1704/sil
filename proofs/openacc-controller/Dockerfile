# syntax=docker/dockerfile:1

# The OpenACC single-FMU adoption example (issue #194): the production SiL
# runtime image plus the consumer material that authors, runs and judges the
# replay. The FMU needs nothing else: its only execution tool, CPython with
# libpython, comes from the runtime image's own base. No exporter, no FMPy
# and no compiler enter this image: the FMU, the recording window and the
# FMPy reference come pinned from the #178 bundle.
ARG SIL_RUNTIME_IMAGE
FROM ${SIL_RUNTIME_IMAGE}

USER root

# The #178 evidence as committed: the pins and the audited interface, and the
# model source the archive must carry.
COPY proofs/public-workloads/evidence/handoff.json \
     proofs/public-workloads/evidence/fmu-audit.json \
     proofs/acc-fmi/models/controller.py \
     /opt/openacc-controller/
COPY proofs/openacc-controller/acceptance.py \
     proofs/openacc-controller/identity.py \
     proofs/openacc-controller/lifecycle.py \
     proofs/openacc-controller/controller_replay.py \
     /opt/openacc-controller/

RUN install -d -o 10001 -g 10001 /bundle

USER 10001:10001
WORKDIR /workspace
