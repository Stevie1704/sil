#!/usr/bin/env bash
# Build/prepare once, then execute the installed matrix with no network.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:-"$root/build/adas-matrix-evidence"}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
if [[ -n "$(ls -A "$evidence")" ]]; then
    echo 'name a new, empty evidence directory' >&2
    exit 2
fi
revision=$(git -C "$root" rev-parse HEAD)
if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
tools_id=$(mktemp)
runtime_id=$(mktemp)
trap 'rm -f "$tools_id" "$runtime_id"' EXIT
args=(--build-arg "PYTHON_IMAGE=${SIL_ADAS_MATRIX_PYTHON_IMAGE:-python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d}")
if [[ -n "${SIL_ADAS_MATRIX_PYTHON_IMAGE:-}" ]]; then
    args+=(--build-arg "PYTHON_IMAGE=$SIL_ADAS_MATRIX_PYTHON_IMAGE")
fi
docker build --platform linux/amd64 -f "$root/proofs/adas-closed-loop/Dockerfile" \
    --build-arg "SOURCE_REVISION=$revision" "${args[@]}" --iidfile "$tools_id" "$root"
docker build --platform linux/amd64 -f "$root/proofs/adas-matrix/Dockerfile" \
    --build-arg "TOOLS_IMAGE=$(cat "$tools_id")" "${args[@]}" \
    --iidfile "$runtime_id" -t sil-adas-matrix:example "$root"
image_id=$(cat "$runtime_id")
container=$(docker create --platform linux/amd64 --network none --init "$image_id")
trap 'docker rm -f "$container" >/dev/null; rm -f "$tools_id" "$runtime_id"' EXIT
status=0
docker start -a "$container" || status=$?
docker cp "$container:/work/evidence/." "$evidence/" || status=1
prepare_id=$(mktemp)
docker build --platform linux/amd64 -f "$root/proofs/adas-matrix/Dockerfile" \
    --target prepare --build-arg "TOOLS_IMAGE=$(cat "$tools_id")" "${args[@]}" \
    --iidfile "$prepare_id" "$root"
preparation=$(docker create --entrypoint /bin/true "$(cat "$prepare_id")")
docker cp "$preparation:/prepared/preparation" "$evidence/preparation" || status=1
docker rm "$preparation" >/dev/null
rm -f "$prepare_id"
printf '%s\n' "$image_id" > "$evidence/image-id.txt"
exit "$status"
