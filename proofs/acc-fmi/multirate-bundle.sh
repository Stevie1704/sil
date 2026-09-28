#!/usr/bin/env bash
# Prepare pinned ACC multi-rate references, then run the installed Linux proof.
# Usage: multirate-bundle.sh prepare|run [bundle-directory] [evidence-directory]
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
mode=${1:-run}
bundle=${2:-"$root/build/acc-multirate-bundle"}
evidence=${3:-"$root/build/acc-multirate-evidence"}
platform=linux/amd64
tools_image=sil-acc-multirate-tools:local
runtime_image=sil-acc-multirate-runtime:local
example_image=sil-acc-multirate-example:local
container=
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi; }
trap cleanup EXIT

if [[ "$mode" == prepare ]]; then
    revision=$(git -C "$root" rev-parse HEAD)
    if [[ -n "$(git -C "$root" status --porcelain)" ]]; then revision="$revision-dirty"; fi
    version=$(python3 "$root/tools/release.py" project-version --root "$root")
    docker buildx build --load --platform "$platform" -f "$root/proofs/acc-fmi/Dockerfile" \
        --build-arg SOURCE_REVISION="$revision" -t "$tools_image" "$root"
    docker buildx build --load --platform "$platform" --target runtime -t "$runtime_image" \
        --build-arg SIL_VERSION="$version" --build-arg SIL_SOURCE_REVISION="$revision" "$root"
    docker buildx build --load --platform "$platform" -f "$root/proofs/acc-fmi/Dockerfile.multirate" \
        --build-arg SIL_RUNTIME_IMAGE="$runtime_image" -t "$example_image" "$root"
    mkdir -p "$bundle"
    bundle=$(cd "$bundle" && pwd)
    if [[ -n "$(ls -A "$bundle")" ]]; then
        echo "$bundle must be empty before preparation" >&2
        exit 2
    fi
    cat > "$bundle/images.json" <<JSON
{
  "tools_id": "$(docker image inspect "$tools_image" --format '{{.Id}}')",
  "runtime_id": "$(docker image inspect "$runtime_image" --format '{{.Id}}')",
  "example_id": "$(docker image inspect "$example_image" --format '{{.Id}}')",
  "platform": "$platform",
  "source_revision": "$revision"
}
JSON
    container=$(docker create --platform "$platform" --network none \
        "$tools_image" python /src/proofs/acc-fmi/multirate_prepare.py /work)
    docker cp "$bundle/images.json" "$container:/work/images.json"
    status=0
    docker start -a "$container" || status=$?
    docker cp "$container:/work/." "$bundle/"
    cleanup
    container=
    if [[ "$status" != 0 ]]; then exit "$status"; fi

elif [[ "$mode" == run ]]; then
    test -f "$bundle/bundle.json"
    bundle=$(cd "$bundle" && pwd)
    mkdir -p "$evidence"
    evidence=$(cd "$evidence" && pwd)
    container=$(docker create --platform "$platform" --network none \
        --entrypoint python3 \
        -e SIL_ACC_PARTICIPANT_TIMEOUT_MS="${SIL_ACC_PARTICIPANT_TIMEOUT_MS:-30000}" \
        -e SIL_ACC_EXAMPLE_IMAGE_ID="$(docker image inspect "$example_image" --format '{{.Id}}')" \
        "$example_image" /opt/acc-multirate/multirate_acceptance.py /bundle /workspace)
    docker cp "$bundle/." "$container:/bundle/"
    docker cp "$bundle/fmus/." "$container:/fmus/"
    status=0
    docker start -a "$container" || status=$?
    docker cp "$container:/workspace/." "$evidence/"
    cleanup
    container=
    if [[ "$status" != 0 ]]; then exit "$status"; fi
else
    echo 'usage: multirate-bundle.sh prepare|run [bundle-directory] [evidence-directory]' >&2
    exit 2
fi
