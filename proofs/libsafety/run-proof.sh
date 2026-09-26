#!/usr/bin/env bash
# Qualify opendbc's safety library against its recorded baseline (issue #193).
#
#   proofs/libsafety/run-proof.sh <bundle-directory> [output-directory]
#
# <bundle-directory> is the #178 offline bundle: proofs/public-workloads/
# run-proof.sh writes it to build/public-workloads/bundle, and CI keeps it as
# the public-workloads-bundle artifact. The output (default build/libsafety)
# receives prepared/ (the exported frames), inputs/, runs/ and evidence/.
#
# The frames are exported in the #178 tool image, which carries opendbc's log
# reader; it is built from proofs/public-workloads/Dockerfile unless it
# exists. The acceptance then runs in the example image: the production
# runtime image plus libubsan1 and this directory's consumer files. Both steps
# run with no network. PYTHON_IMAGE overrides the base image, e.g. with its
# amd64 manifest digest on a host without BuildKit.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
bundle=${1:?usage: run-proof.sh <bundle-directory> [output-directory]}
output=${2:-"$root/build/libsafety"}
platform=linux/amd64
tools_image=sil-public-workloads:qualification
runtime_image=sil-libsafety-runtime:local
example_image=sil-libsafety-example:local
timeout_ms=${SIL_LIBSAFETY_PARTICIPANT_TIMEOUT_MS:-30000}

revision=$(git -C "$root" rev-parse HEAD)
if ! git -C "$root" diff --quiet HEAD; then revision="$revision-dirty"; fi
version=$(python3 "$root/tools/release.py" project-version --root "$root")
base=()
if [[ -n "${PYTHON_IMAGE:-}" ]]; then base=(--build-arg "PYTHON_IMAGE=$PYTHON_IMAGE"); fi

container=
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi; }
trap cleanup EXIT
image_id() { docker image inspect "$1" --format '{{.Id}}'; }

bundle=$(cd "$bundle" && pwd)
rm -rf "$output"
mkdir -p "$output/prepared"
output=$(cd "$output" && pwd)

if ! docker image inspect "$tools_image" >/dev/null 2>&1; then
    docker build --platform "$platform" -f "$root/proofs/public-workloads/Dockerfile" \
        "${base[@]}" --build-arg SOURCE_REVISION="$revision" -t "$tools_image" "$root"
fi
docker build --platform "$platform" --target runtime "${base[@]}" \
    --build-arg SIL_VERSION="$version" --build-arg SIL_SOURCE_REVISION="$revision" \
    -t "$runtime_image" "$root"
docker build --platform "$platform" -f "$root/proofs/libsafety/Dockerfile" \
    --build-arg SIL_RUNTIME_IMAGE="$runtime_image" -t "$example_image" "$root"

cat > "$output/prepared/tool-image.json" <<JSON
{"tag": "$tools_image", "id": "$(image_id "$tools_image")", "platform": "$platform",
 "source_revision": "$revision"}
JSON
container=$(docker create --platform "$platform" --network none "$tools_image" \
    python /opt/libsafety/prepare_frames.py /bundle /sources/opendbc \
    /src/proofs/public-workloads /out)
docker cp "$root/proofs/libsafety/." "$container:/opt/libsafety/"
docker cp "$bundle/." "$container:/bundle/"
docker start -a "$container"
docker cp "$container:/out/." "$output/prepared/"
cleanup

container=$(docker create --platform "$platform" --network none \
    --entrypoint python3 \
    -e SIL_LIBSAFETY_PARTICIPANT_TIMEOUT_MS="$timeout_ms" \
    -e SIL_LIBSAFETY_EXAMPLE_IMAGE_ID="$(image_id "$example_image")" \
    "$example_image" /opt/libsafety/acceptance.py /bundle /prepared /workspace)
docker cp "$bundle/." "$container:/bundle/"
docker cp "$output/prepared/." "$container:/prepared/"
status=0
docker start -a "$container" || status=$?
docker cp "$container:/workspace/." "$output/"
exit "$status"
