#!/usr/bin/env bash
# Qualify opendbc's safety library against its independent reference (#193),
# as a Process participant and as a Native participant with transmit (#232).
#
#   proofs/libsafety/run-proof.sh <bundle-directory> [output-directory]
#
# <bundle-directory> is the #178 offline bundle: proofs/public-workloads/
# run-proof.sh writes it to build/public-workloads/bundle, and CI keeps it as
# the public-workloads-bundle artifact. The output (default build/libsafety)
# receives prepared/ (the exported frames and transmit candidates), inputs/,
# runs/, bundles/, matrix/ and evidence/.
#
# The frames are exported in the #178 tool image, which carries opendbc's log
# reader. It is always built from proofs/public-workloads/Dockerfile at this
# revision (a cache hit after that proof's own build), and the revision baked
# into it must be this checkout's. The acceptance then runs in the example image: the production
# runtime image plus libubsan1 and this directory's consumer files. Both steps
# run with no network. PYTHON_IMAGE overrides the base image, e.g. with its
# amd64 manifest digest on a host without BuildKit.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
bundle=${1:?usage: run-proof.sh <bundle-directory> [output-directory]}
output=${2:-"$root/build/libsafety"}
platform=linux/amd64
tools_image=sil-public-workloads:qualification
build_image=sil-libsafety-build:local
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

docker build --platform "$platform" -f "$root/proofs/public-workloads/Dockerfile" \
    "${base[@]}" --build-arg SOURCE_REVISION="$revision" -t "$tools_image" "$root"
tools_revision=$(docker run --rm --platform "$platform" --network none \
    "$tools_image" cat /src/source-revision.txt)
if [[ "$tools_revision" != "$revision" ]]; then
    echo "tool image $tools_image holds revision $tools_revision, not $revision" >&2
    exit 1
fi
for target in build runtime; do
    image=$build_image
    if [[ "$target" == runtime ]]; then image=$runtime_image; fi
    docker build --platform "$platform" --target "$target" "${base[@]}" \
        --build-arg SIL_VERSION="$version" --build-arg SIL_SOURCE_REVISION="$revision" \
        -t "$image" "$root"
done
docker build --platform "$platform" -f "$root/proofs/libsafety/Dockerfile" \
    --build-arg SIL_BUILD_IMAGE="$build_image" \
    --build-arg SIL_RUNTIME_IMAGE="$runtime_image" -t "$example_image" "$root"

cat > "$output/prepared/tool-image.json" <<JSON
{"tag": "$tools_image", "id": "$(image_id "$tools_image")", "platform": "$platform",
 "source_revision": "$tools_revision"}
JSON
# Two processes: the transmit reference loads the library, whose state is
# C globals, so it runs in a process of its own.
container=$(docker create --platform "$platform" --network none "$tools_image" \
    sh -c 'for step in prepare_frames prepare_transmit; do
               python "/opt/libsafety/$step.py" /bundle /sources/opendbc \
                   /src/proofs/public-workloads /out || exit; done')
docker cp "$root/proofs/libsafety/." "$container:/opt/libsafety/"
docker cp "$bundle/." "$container:/bundle/"
docker start -a "$container"
docker cp "$container:/out/." "$output/prepared/"
cleanup

# The Process form (#193), then the Native form with transmit (#232).
container=$(docker create --platform "$platform" --network none \
    --entrypoint sh \
    -e SIL_LIBSAFETY_PARTICIPANT_TIMEOUT_MS="$timeout_ms" \
    -e SIL_LIBSAFETY_EXAMPLE_IMAGE_ID="$(image_id "$example_image")" \
    "$example_image" -c 'for step in acceptance native_acceptance; do
        python3 "/opt/libsafety/$step.py" /bundle /prepared /workspace || exit; done')
# The example image runs as a non-root user, and upstream's build leaves the
# library with mode 0600 (mkstemp). Copy a readable stage of the bundle in.
stage=$(mktemp -d "$output/bundle-stage.XXXXXX")
cp -R "$bundle/." "$stage/"
chmod -R a+rX "$stage" "$output/prepared"
docker cp "$stage/." "$container:/bundle/"
rm -rf "$stage"
docker cp "$output/prepared/." "$container:/prepared/"
status=0
docker start -a "$container" || status=$?
docker cp "$container:/workspace/." "$output/"
exit "$status"
