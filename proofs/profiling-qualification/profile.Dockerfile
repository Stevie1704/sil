# syntax=docker/dockerfile:1
# The profiling image of docs/container.md ("Profile in a container"),
# the documented one; only the comments differ: the runtime image
# plus perf and Heaptrack, pinned to one Debian snapshot. perf gets
# CAP_PERFMON as a file capability, so the non-root image user can use it in
# a container started with `--cap-add PERFMON`. No host setting changes.
ARG SIL_IMAGE=sil:local
FROM ${SIL_IMAGE}
USER root
ARG DEBIAN_SNAPSHOT=20260901T000000Z
RUN rm -f /etc/apt/sources.list.d/*.sources \
    && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
      linux-perf=6.1.176-1 heaptrack=1.4.0-2 libcap2-bin \
    && setcap cap_perfmon+ep "$(readlink -f /usr/bin/perf)" \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
ENTRYPOINT ["sil"]
