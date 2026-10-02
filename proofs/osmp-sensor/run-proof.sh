#!/usr/bin/env bash
# Build and inspect the OSMP FMU target (issue #230) on Linux x86-64.
# Usage: run-proof.sh [output-directory] (default: build/osmp-sensor).
# The image build fetches and verifies the pinned sources; the FMU build,
# inspection and runs then use no network. The output receives bundle/ and
# evidence/, including on failure. PYTHON_IMAGE overrides the base image, e.g.
# with its amd64 manifest digest on a host without BuildKit.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
output=${1:-"$root/build/osmp-sensor"}
image=sil-osmp-sensor:qualification
revision=$(git -C "$root" rev-parse HEAD)
if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
base=()
if [[ -n "${PYTHON_IMAGE:-}" ]]; then base=(--build-arg "PYTHON_IMAGE=$PYTHON_IMAGE"); fi
docker build --platform linux/amd64 -f "$root/proofs/osmp-sensor/Dockerfile" \
    "${base[@]}" --build-arg SOURCE_REVISION="$revision" -t "$image" "$root"
mkdir -p "$output"
output=$(cd "$output" && pwd)
container=$(docker create --platform linux/amd64 --network none "$image")
trap 'docker rm -f "$container" >/dev/null' EXIT
status=0
docker start -a "$container" || status=$?
docker cp "$container:/work/." "$output/"
docker image inspect "$image" --format '{{.Id}}' > "$output/evidence/image-id.txt" || true
exit "$status"
