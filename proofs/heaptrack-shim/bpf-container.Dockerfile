# Disposable setup probe, run with Docker's default capabilities and seccomp.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
RUN apt-get update \
    && apt-get install -y --no-install-recommends bpftrace=0.20.2-1ubuntu4.3 gcc-12=12.4.0-2ubuntu1~24.04.1 libc6-dev python3 \
    && rm -rf /var/lib/apt/lists/*
ENV CC=gcc-12
COPY proofs/heaptrack-shim/availability.py proofs/heaptrack-shim/evidence.py /proof/
ENTRYPOINT ["python3", "/proof/availability.py"]
CMD ["/work/evidence"]
