#!/usr/bin/env bash
# Build, audit and check the ADAS reference FMU on Linux x86-64 (issue #225).
# Usage: run-proof.sh [evidence-directory] (default: build/adas-fmu-evidence).
# Requires Docker and, for the image build only, network access. The proof
# itself runs without network. Its outputs, also after a failure, are copied
# out of the container, and the container is removed.
# SIL_ADAS_FMU_PYTHON_IMAGE overrides the base image, for a builder that
# cannot select linux/amd64 from the multi-platform index.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:-"$root/build/adas-fmu-evidence"}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
image=sil-adas-fmu:preparation
build_args=()
if [[ -n "${SIL_ADAS_FMU_PYTHON_IMAGE:-}" ]]; then
    build_args=(--build-arg "PYTHON_IMAGE=$SIL_ADAS_FMU_PYTHON_IMAGE")
fi
docker build --platform linux/amd64 -f "$root/proofs/adas-fmu/Dockerfile" \
    "${build_args[@]+"${build_args[@]}"}" -t "$image" "$root"
container=$(docker create --platform linux/amd64 --network none "$image")
trap 'docker rm -f "$container" >/dev/null' EXIT
status=0
docker start -a "$container" || status=$?
docker cp "$container:/work/." "$evidence/"
docker image inspect "$image" --format '{{.Id}}' > "$evidence/image-id.txt"
exit "$status"
