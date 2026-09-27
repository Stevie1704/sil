#!/usr/bin/env bash
# Qualify the public ACC controller FMU on recorded OpenACC data (#194).
#
#   proofs/openacc-controller/run-proof.sh <bundle-directory> [output-directory]
#
# <bundle-directory> is the #178 offline bundle: proofs/public-workloads/
# run-proof.sh writes it to build/public-workloads/bundle, and CI keeps it as
# the public-workloads-bundle artifact. The output (default
# build/openacc-controller) receives inputs/, runs/ and evidence/.
#
# The acceptance runs in the example image, the production runtime image plus
# this directory's consumer files, with no network. PYTHON_IMAGE overrides the
# base image, e.g. with its amd64 manifest digest on a host without BuildKit.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
bundle=${1:?usage: run-proof.sh <bundle-directory> [output-directory]}
output=${2:-"$root/build/openacc-controller"}
platform=linux/amd64
runtime_image=sil-openacc-controller-runtime:local
example_image=sil-openacc-controller-example:local
timeout_ms=${SIL_OPENACC_PARTICIPANT_TIMEOUT_MS:-30000}

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
mkdir -p "$output"
output=$(cd "$output" && pwd)

docker build --platform "$platform" --target runtime "${base[@]}" \
    --build-arg SIL_VERSION="$version" --build-arg SIL_SOURCE_REVISION="$revision" \
    -t "$runtime_image" "$root"
docker build --platform "$platform" -f "$root/proofs/openacc-controller/Dockerfile" \
    --build-arg SIL_RUNTIME_IMAGE="$runtime_image" -t "$example_image" "$root"

container=$(docker create --platform "$platform" --network none \
    --entrypoint python3 \
    -e SIL_OPENACC_PARTICIPANT_TIMEOUT_MS="$timeout_ms" \
    -e SIL_OPENACC_EXAMPLE_IMAGE_ID="$(image_id "$example_image")" \
    "$example_image" /opt/openacc-controller/acceptance.py /bundle /workspace)
# The example image runs as a non-root user: copy a readable stage in.
stage=$(mktemp -d "$output/bundle-stage.XXXXXX")
cp -R "$bundle/." "$stage/"
chmod -R a+rX "$stage"
docker cp "$stage/." "$container:/bundle/"
rm -rf "$stage"
status=0
docker start -a "$container" || status=$?
docker cp "$container:/workspace/." "$output/"
exit "$status"
