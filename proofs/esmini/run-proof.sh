#!/usr/bin/env bash
# Execute the esmini adoption proof end to end and write its evidence.
#
# Everything it runs comes from the published SiL release: the derived image
# is built FROM the runner image by digest, and every SiL command inside it is
# an entry point that image already ships. Nothing is taken from a SiL
# checkout or build directory; this script and the consumer material beside it
# are the only repository files involved, and the kernel never sees them.
#
#   ./run-proof.sh [evidence-dir] [workspace-dir]
#
# The workspace defaults to a temporary directory and holds the Manifests and
# Recordings; the evidence directory defaults to ./evidence and holds what is
# committed. Requires docker and, for the image build, network access.

set -euo pipefail

PROOF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVIDENCE_DIR="${1:-$PROOF_DIR/evidence}"
WORKSPACE="${2:-$(mktemp -d)}"

IMAGE="${SIL_ESMINI_IMAGE:-sil-esmini-proof:local}"
# The published image's platform. The determinism guarantee is scoped to one
# machine class, so the platform is pinned rather than inherited from the host.
PLATFORM="${SIL_ESMINI_PLATFORM:-linux/amd64}"
# Wall-clock deadline for each Process-participant request. esmini parses the
# OpenDRIVE road network inside its initialization, which is the slowest
# single request in the Run.
PARTICIPANT_TIMEOUT_MS="${SIL_ESMINI_PARTICIPANT_TIMEOUT_MS:-30000}"

# The one place the base image digest is written down is the Dockerfile.
SIL_IMAGE="$(sed -n 's/^FROM \(ghcr\.io[^ ]*\) AS consumer$/\1/p' "$PROOF_DIR/Dockerfile")"

# The Channels and KPI thresholds are not repeated here. `verify.py` reads
# them back out of the Manifest the Run was executed from, so the post-hoc
# judgement cannot drift from the one the Run enforced.
SCENARIO=/opt/esmini/resources/xosc/alks_r157_cut_in_quick_brake.xosc

mkdir -p "$EVIDENCE_DIR" "$WORKSPACE"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

docker_run() {
    docker run --rm --platform "$PLATFORM" --network none \
        --user "$(id -u):$(id -g)" \
        --mount "type=bind,src=$WORKSPACE,dst=/workspace" \
        --workdir /workspace "$@"
}

# `sil-run` is the image's entry point, so a Run needs no override.
sil_run() { docker_run "$IMAGE" "$@"; }
# Everything else the image ships is named explicitly.
sil_tool() { local tool="$1"; shift; docker_run --entrypoint "$tool" "$IMAGE" "$@"; }

step() { printf '\n=== %s ===\n' "$1"; }

step "Build the derived consumer image"
docker build --platform "$PLATFORM" --tag "$IMAGE" "$PROOF_DIR"

step "Record the artifact identities"
{
    echo "platform                 $PLATFORM"
    echo "host                     $(uname -s) $(uname -m)"
    echo "sil runner image         $SIL_IMAGE"
    echo "sil version              $(sil_tool sil-run --version | tr -d '\r')"
    echo "derived image id         $(docker image inspect --format '{{.Id}}' "$IMAGE")"
    echo "esmini version           $(sil_tool cat /opt/esmini/version.txt | tr '\n' ' ')"
    sed -n 's/^ARG \(ESMINI_[A-Z0-9_]*\)=\(.*\)$/\1 \2/p' "$PROOF_DIR/Dockerfile" \
        | awk '{ printf "%-24s %s\n", tolower($1), $2 }'
    echo "participant timeout ms   $PARTICIPANT_TIMEOUT_MS"
} | tee "$EVIDENCE_DIR/identity.txt"

step "Shared libraries the consumer artifact needs at load time"
sil_tool ldd /opt/esmini/bin/libesminiLib.so \
    | tee "$EVIDENCE_DIR/esmini-runtime-libs.txt"
if grep -q "not found" "$EVIDENCE_DIR/esmini-runtime-libs.txt"; then
    echo "the derived image is missing a runtime dependency of esmini" >&2
    exit 1
fi

step "Build both Manifests with the released builder"
nominal_hash="$(sil_tool python3 /opt/consumer/manifest.py /workspace/alks-cut-in.json)"
failing_hash="$(sil_tool python3 /opt/consumer/manifest.py --unmeetable-gap \
    /workspace/alks-cut-in-failing.json)"
if [ "$nominal_hash" = "$failing_hash" ]; then
    echo "the two variants hash the same; they are not two Runs" >&2
    exit 1
fi
cp "$WORKSPACE/alks-cut-in.json" "$WORKSPACE/alks-cut-in-failing.json" "$EVIDENCE_DIR/"
{
    echo "nominal  $nominal_hash"
    echo "failing  $failing_hash"
} | tee "$EVIDENCE_DIR/manifest-hashes.txt"

step "Declared payload footprint"
sil_tool sil-footprint /workspace/alks-cut-in.json | tee "$EVIDENCE_DIR/footprint.txt"
if grep -q "unbounded" "$EVIDENCE_DIR/footprint.txt"; then
    echo "a subscriber route declares no capacity" >&2
    exit 1
fi

step "Run the proof, measuring what it costs"
sil_tool python3 /opt/consumer/observe.py /workspace/resources.json \
    sil-run /workspace/alks-cut-in.json \
    --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS" \
    -o /workspace/run-1.mcap
cp "$WORKSPACE/resources.json" "$EVIDENCE_DIR/resources.json"
cat "$EVIDENCE_DIR/resources.json"

step "Audit the vendor expectation against esmini's own smoke test"
sil_tool python3 /opt/consumer/reference/check_transcription.py \
    --reference /opt/consumer/reference/alks_r157_expected.json \
    --smoke-test /opt/esmini/test/smoke_test.py \
    --scenario "$SCENARIO" \
    --resources /opt/esmini/resources \
    --library /opt/esmini/bin/libesminiLib.so \
    --object-id Ego=300 --object-id Target=301 \
    | tee "$EVIDENCE_DIR/reference-provenance.txt"

step "Verify the Recording, the KPI, and the vendor's expected trajectory"
sil_tool python3 /opt/consumer/verify.py \
    --recording /workspace/run-1.mcap \
    --manifest /workspace/alks-cut-in.json \
    --reference /opt/consumer/reference/alks_r157_expected.json \
    --observations /workspace/observations.json
cp "$WORKSPACE/observations.json" "$EVIDENCE_DIR/observations.json"

step "Determinism check"
sil_run /workspace/alks-cut-in.json \
    --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS" -o /workspace/run-2.mcap
first="$(sha256_of "$WORKSPACE/run-1.mcap")"
second="$(sha256_of "$WORKSPACE/run-2.mcap")"
{
    echo "run-1.mcap  $first"
    echo "run-2.mcap  $second"
    echo "bytes       $(wc -c <"$WORKSPACE/run-1.mcap" | tr -d ' ')"
} | tee "$EVIDENCE_DIR/determinism.txt"
if [ "$first" != "$second" ]; then
    echo "DETERMINISM VIOLATION: the two Recordings differ" >&2
    exit 1
fi
# The Recording is an artifact of the proof, not a by-product of it: #117 asks
# for it to be retained, so it is kept beside the Manifests it came from and
# not left in a temporary workspace.
cp "$WORKSPACE/run-1.mcap" "$EVIDENCE_DIR/run-1.mcap"
# The released determinism check runs the same comparison from inside the
# image. It has no way to pass a Process-participant deadline, so both are
# run: this one for the shipped tool, the pair above for the deadline.
sil_tool sil-check /workspace/alks-cut-in.json --runner sil-run \
    | tee -a "$EVIDENCE_DIR/determinism.txt"

step "Clock-shim control"
# Declaring the shim is not evidence that it does anything. The same Run
# without it answers whether esmini ever reads the wall clock at all.
sil_tool python3 /opt/consumer/manifest.py --no-shim \
    /workspace/alks-cut-in-unshimmed.json >/dev/null
sil_run /workspace/alks-cut-in-unshimmed.json \
    --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS" \
    -o /workspace/run-unshimmed.mcap
sil_tool python3 /opt/consumer/clock_shim_control.py \
    --manifest /workspace/alks-cut-in.json \
    --with-shim /workspace/run-1.mcap \
    --without-shim /workspace/run-unshimmed.mcap \
    | tee "$EVIDENCE_DIR/clock-shim-control.json"

step "Deliberately failing variant"
set +e
failing_output="$(sil_run /workspace/alks-cut-in-failing.json \
    --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS" \
    -o /workspace/run-failing.mcap 2>&1)"
failing_code=$?
set -e
{
    echo "exit code  $failing_code"
    echo "--- diagnostic ---"
    echo "$failing_output"
} | tee "$EVIDENCE_DIR/failing-variant.txt"
if [ "$failing_code" -ne 1 ]; then
    echo "the failing variant exited $failing_code, not the Run-failure 1" >&2
    exit 1
fi
if ! grep -q "freespace gap" "$EVIDENCE_DIR/failing-variant.txt"; then
    echo "the failing variant's diagnostic does not name the KPI" >&2
    exit 1
fi

step "Manifest-error path"
# The other half of the exit-code taxonomy: a Manifest that names an
# environment the Participant cannot honour is rejected before anything is
# stepped, and stays distinct from the Run failure above.
sil_tool python3 /opt/consumer/manifest.py \
    --scenario "$(dirname "$SCENARIO")/does-not-exist.xosc" \
    /workspace/missing-scenario.json >/dev/null
set +e
missing_output="$(sil_run /workspace/missing-scenario.json \
    --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS" \
    -o /workspace/run-missing.mcap 2>&1)"
missing_code=$?
set -e
{
    echo "exit code  $missing_code"
    echo "--- diagnostic ---"
    echo "$missing_output"
} | tee "$EVIDENCE_DIR/manifest-error.txt"
if [ "$missing_code" -ne 2 ]; then
    echo "the missing scenario exited $missing_code, not the Manifest-error 2" >&2
    exit 1
fi

printf '\nproof complete; Manifests and Recordings are in %s\n' "$WORKSPACE"
