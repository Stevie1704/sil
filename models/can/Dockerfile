FROM python@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git patch ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fmpy==0.3.32 pytest==8.4.2
RUN git clone https://github.com/modelica/fmi-ls-bus-examples.git /opt/examples \
    && git -C /opt/examples checkout de019a6efbad810795835f2fd9bbf9e62eb451b9 \
    && git clone https://github.com/modelica/fmi-ls-bus.git /opt/spec \
    && git -C /opt/spec checkout 8abdf039bfb994c794e4c15bce575cfc00a1ab6e
WORKDIR /work

RUN pip install --no-cache-dir mcap==1.3.1
COPY . /opt/sil-source
RUN cmake -S /opt/sil-source -B /opt/kernel -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /opt/kernel --target sil-run -j2
