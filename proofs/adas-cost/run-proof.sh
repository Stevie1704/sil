#!/usr/bin/env bash
# Measure the ADAS reference workload in its native, process-isolated
# C-library and FMU forms on Linux x86-64 (issue #229).
# Usage: run-proof.sh [evidence-directory] [measure.py options]
# (default: build/adas-cost-evidence). Requires Docker and, for the image
# build only, network access. The measurement itself runs without network.
# Its outputs, also after a failure, are copied out of the container, and the
# container is removed.
# SIL_ADAS_COST_PYTHON_IMAGE overrides the base image, for a builder that
# cannot select linux/amd64 from the multi-platform index.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:-"$root/build/adas-cost-evidence"}
shift || true
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
image=sil-adas-cost:proof
build_args=()
if [[ -n "${SIL_ADAS_COST_PYTHON_IMAGE:-}" ]]; then
    build_args=(--build-arg "PYTHON_IMAGE=$SIL_ADAS_COST_PYTHON_IMAGE")
fi
# The immutable image ID, taken from this build: the tag can move under a
# concurrent build, so the container and the evidence both use the ID.
iidfile=$(mktemp)
docker build --platform linux/amd64 -f "$root/proofs/adas-cost/Dockerfile" \
    "${build_args[@]+"${build_args[@]}"}" --iidfile "$iidfile" -t "$image" "$root"
image_id=$(cat "$iidfile")
rm -f "$iidfile"
# --init reaps every participant process; no network is needed to measure.
container=$(docker create --platform linux/amd64 --network none --init \
    "$image_id" python /src/proofs/adas-cost/measure.py /work "$@")
trap 'docker rm -f "$container" >/dev/null' EXIT
status=0
docker start -a "$container" || status=$?
docker cp "$container:/work/." "$evidence/"
echo "$image_id" > "$evidence/image-id.txt"
exit "$status"
