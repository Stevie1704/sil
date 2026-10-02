#!/usr/bin/env bash
# Run the OSMP FMU qualification (issue #233) on Linux x86-64.
# Usage: run-proof.sh [evidence-directory] (default: build/osmp-qualification).
# The image build fetches and verifies the pinned OSMP sources and prepares
# the bundles; the matrices and the cost measurement use no network. The
# evidence directory receives the runtime evidence even on failure, and the
# preparation report in preparation/.
# PYTHON_IMAGE overrides the base image, e.g. with its amd64 manifest digest
# on a host without BuildKit.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:-"$root/build/osmp-qualification"}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
if [[ -n "$(ls -A "$evidence")" ]]; then
    echo 'name a new, empty evidence directory' >&2
    exit 2
fi
revision=$(git -C "$root" rev-parse HEAD)
if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
args=(--platform linux/amd64 -f "$root/proofs/osmp-qualification/Dockerfile"
      --build-arg "SOURCE_REVISION=$revision")
if [[ -n "${PYTHON_IMAGE:-}" ]]; then args+=(--build-arg "PYTHON_IMAGE=$PYTHON_IMAGE"); fi
runtime_id=$(mktemp)
prepare_id=$(mktemp)
container=
preparation=
cleanup() {
    if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi
    if [[ -n "$preparation" ]]; then docker rm -f "$preparation" >/dev/null; fi
    rm -f "$runtime_id" "$prepare_id"
}
trap cleanup EXIT
docker build "${args[@]}" --iidfile "$runtime_id" -t sil-osmp-qualification:example "$root"
docker build "${args[@]}" --target prepare --iidfile "$prepare_id" "$root"
container=$(docker create --platform linux/amd64 --network none --init "$(cat "$runtime_id")")
status=0
docker start -a "$container" || status=$?
docker cp "$container:/work/evidence/." "$evidence/" || status=1
preparation=$(docker create --platform linux/amd64 --entrypoint /bin/true "$(cat "$prepare_id")")
docker cp "$preparation:/prepared/preparation" "$evidence/preparation" || status=1
cat "$runtime_id" > "$evidence/image-id.txt"
exit "$status"
