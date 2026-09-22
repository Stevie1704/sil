#!/usr/bin/env bash
# Build and qualify the checkout's ACC FMUs through SiL and independent FMPy.
# Usage: run-proof.sh [evidence-directory] (default: build/acc-fmi-evidence).
# Requires Docker, Git and network access for the pinned image/dependencies.
# Execution itself has no network. All outputs, including failures, are copied
# from the container; the container is removed and its exit code propagated.
# SIL_ACC_PARTICIPANT_TIMEOUT_MS overrides the 30000 ms response deadline.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:-"$root/build/acc-fmi-evidence"}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
revision=$(git -C "$root" rev-parse HEAD)
if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
image=sil-acc-fmi:qualification
docker build --platform linux/amd64 -f "$root/proofs/acc-fmi/Dockerfile" \
    --build-arg SOURCE_REVISION="$revision" -t "$image" "$root"
docker image inspect "$image" --format '{{.Id}}' > "$evidence/image-id.txt"
container=$(docker create --platform linux/amd64 --network none \
    -e SIL_ACC_PARTICIPANT_TIMEOUT_MS="${SIL_ACC_PARTICIPANT_TIMEOUT_MS:-30000}" "$image")
cleanup() { docker rm -f "$container" >/dev/null; }
trap cleanup EXIT
status=0
docker start -a "$container" || status=$?
docker cp "$container:/work/." "$evidence/"
exit "$status"
