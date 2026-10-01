#!/usr/bin/env bash
# Close the processed-sensor loop with the native and the FMU controller over
# the ACC plant FMU, on Linux x86-64 (issue #227).
# Usage: run-proof.sh [evidence-directory] (default: build/adas-closed-loop-evidence).
# Requires Docker, Git and, for the image build only, network access. The
# proof itself runs without network. Its outputs, also after a failure, are
# copied out of the container, and the container is removed.
# SIL_ADAS_CLOSED_LOOP_PYTHON_IMAGE overrides the base image, for a builder
# that cannot select linux/amd64 from the multi-platform index.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:-"$root/build/adas-closed-loop-evidence"}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
revision=$(git -C "$root" rev-parse HEAD)
if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
image=sil-adas-closed-loop:proof
build_args=(--build-arg "SOURCE_REVISION=$revision")
if [[ -n "${SIL_ADAS_CLOSED_LOOP_PYTHON_IMAGE:-}" ]]; then
    build_args+=(--build-arg "PYTHON_IMAGE=$SIL_ADAS_CLOSED_LOOP_PYTHON_IMAGE")
fi
# The immutable image ID, taken from this build: the tag can move under a
# concurrent build, so the container and the evidence both use the ID.
iidfile=$(mktemp)
docker build --platform linux/amd64 -f "$root/proofs/adas-closed-loop/Dockerfile" \
    "${build_args[@]}" --iidfile "$iidfile" -t "$image" "$root"
image_id=$(cat "$iidfile")
rm -f "$iidfile"
# --init reaps every process the proof stops, also an orphaned participant.
container=$(docker create --platform linux/amd64 --network none --init "$image_id")
trap 'docker rm -f "$container" >/dev/null' EXIT
status=0
docker start -a "$container" || status=$?
docker cp "$container:/work/." "$evidence/"
echo "$image_id" > "$evidence/image-id.txt"
exit "$status"
