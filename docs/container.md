# Container deployment

[Documentation index](README.md) · [Repository overview](../README.md)

## Run one Manifest in a Linux container

Use the published runtime image from the release notes, pinned by its digest.
Give it a local name for the commands below; no source checkout is needed:

```sh
docker pull ghcr.io/stevie1704/sil@sha256:<release-image-digest>
docker tag ghcr.io/stevie1704/sil@sha256:<release-image-digest> sil:local
```

Mount a workspace at `/workspace` and use the host user's numeric identity so
ordinary host-owned directories remain writable. Runtime networking is not
needed. The image entry point is `sil-run`, so its arguments are the installed
runner's normal command-line contract:

```sh
workspace=$PWD/run
mkdir -p "$workspace"
cp manifest.json "$workspace/manifest.json"

docker run --rm --network none \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  sil:local /workspace/manifest.json \
  --participant-timeout-ms 5000 -o /workspace/out.mcap
```

The command prints the Manifest hash, returns `sil-run`'s exit code unchanged,
and writes `out.mcap` directly into the host directory. To run without a
Recording while keeping the same Manifest and Manifest hash:

```sh
docker run --rm --network none \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  sil:local /workspace/manifest.json \
  --participant-timeout-ms 5000 --no-recording
```

A private image can add Participant artifacts without changing the production
runtime surface. Name their image paths in the mounted Manifest:

```dockerfile
FROM sil:local
USER root
COPY --chown=10001:10001 participants/ /opt/participants/
USER 10001:10001
```

The image also contains the user docs of its version. Read them without a
checkout or network access:

```sh
docker run --rm --network none --entrypoint sil sil:local docs
docker run --rm --network none --entrypoint sil sil:local docs agent-guide
docker run --rm --network none --entrypoint sil sil:local examples
```

Export the installed example sources into a writable mounted workspace with
`--entrypoint sil ... examples --dir /workspace/examples`. Building C examples
uses a host or development environment with a compiler and, for Native
participants, the native development archive's headers.

The default image user is non-root. Callers may still select their host UID and
GID as above; the installed runner and Python Participant entry points do not
depend on a passwd entry or a writable home directory.

## Profile in a container

The runtime image has no profiler. To use `sil profile`
([docs/running.md](running.md#profile-a-run)), derive an image that adds
perf and Heaptrack from the same Debian release as the image, pinned to one
snapshot. perf gets `CAP_PERFMON` as a file capability, so the non-root
image user can sample its own Run:

```dockerfile
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
```

```sh
docker build -t sil-profile:local -f profile.Dockerfile .
docker run --rm --network none --cap-add PERFMON \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$workspace,dst=/workspace" \
  sil-profile:local profile /workspace/manifest.json \
  --out /workspace/prof --collector cpu --participant-timeout-ms 5000
```

- `--cap-add PERFMON` is the only extra privilege, and no host setting
  changes: Docker's default seccomp profile allows `perf_event_open` with
  this capability, and the capability passes the kernel's
  `perf_event_paranoid` check. Do not use `--privileged` or
  `--security-opt seccomp=unconfined`.
- Without `--cap-add PERFMON`, perf cannot start in this image (exec of a
  file with a missing file capability fails): `sil profile --collector cpu`
  stops before the Run with exit `1` and names the capability.
- `--collector heap` needs no capability.
- perf, the Run and `sil profile` are in one container, so in one PID
  namespace: the Activation join is verified there. perf on the host that
  samples a Run in a container is not supported.
- `--out` must be on a file system with space for `--max-capture-bytes`
  and, for the heap collector, one that allows execution (not a `noexec`
  `tmpfs`).
- Render a copied profile offline with the runtime image itself, which has
  no profiler: `docker run --rm --network none --entrypoint sil sil:local
  profile render /workspace/prof/profile.json --html /workspace/prof.html`,
  with the `--user` and `--mount` options above.

The [#319 qualification](../proofs/profiling-qualification/README.md)
qualified this image on Linux x86-64.

## Build an image from a checkout

Contributors can build the production image from its pinned base-image digest
and exact Python dependency lock:

```sh
version="$(python3 tools/release.py project-version)"
docker build --target runtime -t sil:local \
  --build-arg SIL_VERSION="$version" \
  --build-arg SIL_SOURCE_REVISION=local .
```

The separate `acceptance` target adds the public FMU reference artifact for
this repository's CI checks. It is not needed for adopter workflows.
