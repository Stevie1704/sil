# Container deployment

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Run one Manifest in a Linux container

Build the production image from its pinned base-image digest and exact Python
dependency lock. The separate `acceptance` target adds the public FMU reference
artifact for this repository's CI checks; it is not part of the production
image.

```sh
version="$(python3 tools/release.py project-version)"
docker build --target runtime -t sil:local \
  --build-arg SIL_VERSION="$version" \
  --build-arg SIL_SOURCE_REVISION=local .
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

The default image user is non-root. Callers may still select their host UID and
GID as above; the installed runner and Python Participant entry points do not
depend on a passwd entry or a writable home directory.
