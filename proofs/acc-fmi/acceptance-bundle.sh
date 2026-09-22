#!/usr/bin/env bash
# The ACC consumer acceptance bundle (issue #151).
#
#   proofs/acc-fmi/acceptance-bundle.sh prepare [bundle-directory]
#   proofs/acc-fmi/acceptance-bundle.sh run     [bundle-directory] [evidence-directory]
#
# `prepare` is the one command that needs the network. It builds the pinned
# tool image, the production runtime image and the example image, then writes
# the model artifacts, audits, authored configurations and independent
# trajectories into the bundle with a digest for each one.
#
# `run` executes the acceptance checks from that bundle inside the example
# image with no network and no source tree: SiL comes from the installed
# wheel and the installed runner. Artifacts are copied in and evidence out
# with `docker cp`, so no host directory is bind-mounted.
#
# SIL_ACC_PARTICIPANT_TIMEOUT_MS bounds each Participant response (30000 ms).
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
mode=${1:-run}
bundle=${2:-"$root/build/acc-acceptance-bundle"}
evidence=${3:-"$root/build/acc-acceptance-evidence"}
platform=linux/amd64
tools_image=sil-acc-acceptance-tools:local
runtime_image=sil-acc-acceptance-runtime:local
example_image=sil-acc-acceptance-example:local
timeout_ms=${SIL_ACC_PARTICIPANT_TIMEOUT_MS:-30000}

container=
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi; }
trap cleanup EXIT

image_id() { docker image inspect "$1" --format '{{.Id}}'; }

prepare() {
    local revision version
    revision=$(git -C "$root" rev-parse HEAD)
    if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
    version=$(python3 "$root/tools/release.py" project-version --root "$root")

    # The exporter and the independent importer live here and nowhere else.
    docker build --platform "$platform" -f "$root/proofs/acc-fmi/Dockerfile" \
        --build-arg SOURCE_REVISION="$revision" -t "$tools_image" "$root"
    # The production runtime, unchanged, and the example image derived from it.
    docker build --platform "$platform" --target runtime -t "$runtime_image" \
        --build-arg SIL_VERSION="$version" \
        --build-arg SIL_SOURCE_REVISION="$revision" "$root"
    docker build --platform "$platform" -f "$root/proofs/acc-fmi/Dockerfile.bundle" \
        --build-arg SIL_RUNTIME_IMAGE="$runtime_image" -t "$example_image" "$root"

    # Preparation replaces the bundle: a half-regenerated one would carry
    # digests from two revisions. Refuse anything that is not one.
    if [[ -e "$bundle" && ! -f "$bundle/bundle.json" ]] \
        && [[ -n "$(ls -A "$bundle" 2>/dev/null)" ]]; then
        echo "$bundle exists and is not a prepared bundle; name an empty directory" >&2
        exit 2
    fi
    rm -rf "$bundle"
    mkdir -p "$bundle"
    bundle=$(cd "$bundle" && pwd)
    cat > "$bundle/images.json" <<JSON
{
  "tools": {"tag": "$tools_image", "id": "$(image_id "$tools_image")"},
  "runtime": {"tag": "$runtime_image", "id": "$(image_id "$runtime_image")"},
  "example": {"tag": "$example_image", "id": "$(image_id "$example_image")"},
  "platform": "$platform",
  "sil_version": "$version",
  "source_revision": "$revision"
}
JSON
    container=$(docker create --platform "$platform" --network none \
        -e SIL_ACC_PARTICIPANT_TIMEOUT_MS="$timeout_ms" \
        "$tools_image" python /src/proofs/acc-fmi/acceptance_prepare.py /work)
    docker cp "$bundle/images.json" "$container:/work/images.json"
    local status=0
    docker start -a "$container" || status=$?
    docker cp "$container:/work/." "$bundle/"
    cleanup
    container=
    return "$status"
}

execute() {
    if [[ ! -f "$bundle/bundle.json" ]]; then
        echo "no prepared bundle in $bundle; run 'acceptance-bundle.sh prepare' first" >&2
        exit 2
    fi
    bundle=$(cd "$bundle" && pwd)
    local image
    image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["example"]["tag"])' \
        "$bundle/images.json")
    mkdir -p "$evidence"
    evidence=$(cd "$evidence" && pwd)
    container=$(docker create --platform "$platform" --network none \
        --entrypoint python3 \
        -e SIL_ACC_PARTICIPANT_TIMEOUT_MS="$timeout_ms" \
        -e SIL_ACC_EXAMPLE_IMAGE_ID="$(image_id "$image")" \
        "$image" /opt/acc-example/acceptance.py /bundle /work)
    docker cp "$bundle/." "$container:/bundle/"
    docker cp "$bundle/fmus/." "$container:/fmus/"
    local status=0
    docker start -a "$container" || status=$?
    docker cp "$container:/work/." "$evidence/"
    cp "$bundle/images.json" "$evidence/images.json"
    cleanup
    container=
    return "$status"
}

case "$mode" in
    prepare) prepare ;;
    run) execute ;;
    *) echo "usage: acceptance-bundle.sh prepare|run [bundle-directory] [evidence-directory]" >&2
       exit 2 ;;
esac
