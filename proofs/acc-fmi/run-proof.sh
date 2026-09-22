#!/usr/bin/env bash
# Build and qualify the checkout's ACC FMUs through SiL and independent FMPy.
# Usage: run-proof.sh [evidence-directory] (default: build/acc-fmi-evidence).
# Requires Docker, Git and network access for the pinned image/dependencies.
# Execution itself has no network. All outputs, including failures, are copied
# from each container; containers are removed and failure status propagated.
# SIL_ACC_PARTICIPANT_TIMEOUT_MS overrides the 30000 ms response deadline.
# SIL_ACC_PROOF: qualify (default), closed_loop, sensitivity, or all (one build).
# With all, evidence goes into qualify/, closed_loop/, and sensitivity/ below
# the destination.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:-"$root/build/acc-fmi-evidence"}
proof=${SIL_ACC_PROOF:-qualify}
case "$proof" in
    qualify|closed_loop|sensitivity) modes=("$proof") ;;
    all) modes=(qualify closed_loop sensitivity) ;;
    *) echo "SIL_ACC_PROOF must be qualify, closed_loop, sensitivity or all" >&2; exit 2 ;;
esac
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
revision=$(git -C "$root" rev-parse HEAD)
if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
image=sil-acc-fmi:qualification
docker build --platform linux/amd64 -f "$root/proofs/acc-fmi/Dockerfile" \
    --build-arg SOURCE_REVISION="$revision" -t "$image" "$root"
container=
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi; }
trap cleanup EXIT
status=0
for mode in "${modes[@]}"; do
    destination=$evidence
    if [[ "$proof" == all ]]; then destination="$evidence/$mode"; fi
    mkdir -p "$destination"
    container=$(docker create --platform linux/amd64 --network none \
        -e SIL_ACC_PARTICIPANT_TIMEOUT_MS="${SIL_ACC_PARTICIPANT_TIMEOUT_MS:-30000}" \
        "$image" python "/src/proofs/acc-fmi/$mode.py" /work)
    docker start -a "$container" || status=$?
    docker cp "$container:/work/." "$destination/"
    docker image inspect "$image" --format '{{.Id}}' > "$destination/image-id.txt"
    if [[ -d "$destination/curated" ]]; then
        cp "$destination/image-id.txt" "$destination/curated/image-id.txt"
    fi
    cleanup
    container=
done
exit "$status"
