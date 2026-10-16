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
