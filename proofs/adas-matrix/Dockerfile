# syntax=docker/dockerfile:1
ARG TOOLS_IMAGE=sil-adas-matrix-tools:prepare
ARG PYTHON_IMAGE=python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
FROM ${TOOLS_IMAGE} AS prepare
COPY proofs/adas-matrix/prepare.py /src/proofs/adas-matrix/prepare.py
RUN python /src/proofs/adas-matrix/prepare.py /prepared \
    --plant /fmus/AccPlant.fmu --fmpy-python /usr/local/bin/python

# Fresh interpreter and loader: no source checkout, compiler, FMPy or exporter.
FROM ${PYTHON_IMAGE} AS runtime
COPY --from=prepare /opt/sil/native /opt/sil/native
COPY --from=prepare /opt/sil/python /opt/sil/python
COPY --from=prepare /bundles /bundles
COPY --from=prepare /prepared/expected.json /opt/adas/expected.json
COPY proofs/adas-matrix/run.py /opt/adas/run.py
ENV PATH=/opt/sil/python/bin:/opt/sil/native/bin \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python /opt/adas/run.py --seal
RUN ["/bin/sh", "-c", "/bin/mkdir -p /work && /bin/chown -R 10001:10001 /work"]
WORKDIR /work
USER 10001:10001
CMD ["python", "/opt/adas/run.py"]
