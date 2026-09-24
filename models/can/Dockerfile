FROM python@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fmpy==0.3.32 pytest==8.4.2
COPY models/can/profile.json models/can/fetch_upstream.py /opt/can-build/
RUN python /opt/can-build/fetch_upstream.py
WORKDIR /work

RUN pip install --no-cache-dir mcap==1.3.1
COPY . /opt/sil-source
ARG SIL_SOURCE_REVISION=unknown
RUN cmake -S /opt/sil-source -B /opt/kernel -DCMAKE_BUILD_TYPE=Release \
    -DSIL_SOURCE_REVISION="$SIL_SOURCE_REVISION" \
    && cmake --build /opt/kernel --target sil-run -j2
