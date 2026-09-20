#!/usr/bin/env bash
# Execute the FMI-LS-BUS CAN acceptance fixture and write its evidence.
#
#   ./run-proof.sh [evidence-dir] [workspace-dir]
#
# Three things happen here, in this order, and each one is evidence on its own:
#
#   1. the fixture is built from the pinned upstream revision, and the
#      revision this fixture rejected is built beside it;
#   2. both are inspected, and both are executed by an independent FMI 3.0
#      importer against expectations written before any Run;
#   3. the released SiL FMI Importer is pointed at the same FMU, and what it
#      answers is kept verbatim.
#
# The rejected revision's exchange and step 3 are both expected to fail. This
# issue is an evidence gate: the fixture is what the implementation issues are
# measured against, and those failures are the measurement. The script fails
# only when the fixture itself is wrong — a digest that does not match the
# pin, a reference exchange that does not match its expectation, or a rejected
# revision that turns out to work after all.
#
# The evidence directory defaults to ./evidence and holds what is committed;
# the workspace defaults to a temporary directory and holds the Manifests.
# Requires docker and, for the image build, network access. Every step after
# the build runs with no network at all.

set -euo pipefail

PROOF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVIDENCE_DIR="${1:-$PROOF_DIR/evidence}"
WORKSPACE="${2:-$(mktemp -d)}"

FIXTURE_IMAGE="${SIL_LSBUS_FIXTURE_IMAGE:-sil-lsbus-fixture:local}"
PROOF_IMAGE="${SIL_LSBUS_PROOF_IMAGE:-sil-lsbus-proof:local}"
# The determinism guarantee is scoped to one machine class, so the platform is
# pinned rather than inherited from the host.
PLATFORM="${SIL_LSBUS_PLATFORM:-linux/amd64}"
# Wall-clock deadline for each Process-participant request. Nothing in these
# Runs is slow; the deadline is declared so a Run that hangs fails instead.
PARTICIPANT_TIMEOUT_MS="${SIL_LSBUS_PARTICIPANT_TIMEOUT_MS:-10000}"

# The base images and the upstream revisions are written down in the
# Dockerfile, and read back from it here so this script carries no second copy
# of them.
SIL_IMAGE="$(sed -n 's/^FROM \(ghcr\.io[^ ]*\) AS proof$/\1/p' "$PROOF_DIR/Dockerfile")"

NODE_FMU=/opt/fixture/DemoCanNodeTriggeredOutput.fmu
BUS_FMU=/opt/fixture/DemoCanBusSimulation.fmu
# The same node, built from the upstream revision this fixture did not select.
ALTERNATIVE_NODE_FMU=/opt/alternative/DemoCanNodeTriggeredOutput.fmu

# The fixture's identity, pinned twice over.
#
# The fixture digest is one digest over every FMU member that comes from
# upstream, compiled binaries excluded, so it says what upstream contributed
# and nothing about a compiler. The archive digests below are the built FMUs
# as executed, binaries and all; the image build proves them reproducible by
# building the same revision twice and comparing the archives byte for byte.
# A build that reproduces neither is not this fixture, and the proof stops
# rather than report about another one.
EXPECTED_FIXTURE_DIGEST=9974aa8e4c9e1b50e43d7433bdaef72e14b5d9e23997e1600a9725c92c5148e3
EXPECTED_ARCHIVE_DIGESTS="\
58ff71a28bb2a9b6021bbf7d7d286f0a219056ad772ead7ecef1e2ba2bca71c1  $NODE_FMU
c475f76c0530d5a5aaf38a5bfde0ad31d2bfb2b48a0cf1ea6dd9bb38649fa041  $BUS_FMU"


mkdir -p "$EVIDENCE_DIR" "$WORKSPACE"

docker_run() {
    docker run --rm --platform "$PLATFORM" --network none \
        --user "$(id -u):$(id -g)" \
        --mount "type=bind,src=$WORKSPACE,dst=/workspace" \
        --workdir /workspace "$@"
}

# The harness image runs the independent importer; the proof image runs SiL.
harness() { docker_run --entrypoint python "$FIXTURE_IMAGE" "$@"; }
# `sil-run` is the released image's entry point, so a Run needs no override.
sil_run() { docker_run "$PROOF_IMAGE" "$@"; }
sil_tool() { local tool="$1"; shift; docker_run --entrypoint "$tool" "$PROOF_IMAGE" "$@"; }

step() { printf '\n=== %s ===\n' "$1"; }

step "Build the fixture image and the proof image"
docker build --platform "$PLATFORM" --target fixture \
    --tag "$FIXTURE_IMAGE" "$PROOF_DIR"
docker build --platform "$PLATFORM" --target proof \
    --tag "$PROOF_IMAGE" "$PROOF_DIR"

step "Keep the upstream build logs"
# What it took to turn each upstream revision into a loadable FMU on this
# machine class.
docker_run --entrypoint cat "$FIXTURE_IMAGE" /opt/fixture-build.log \
    > "$EVIDENCE_DIR/fixture-build.log"
docker_run --entrypoint cat "$FIXTURE_IMAGE" /opt/repeat-build.log \
    > "$EVIDENCE_DIR/repeat-build.log"
docker_run --entrypoint cat "$FIXTURE_IMAGE" /opt/alternative-build.log \
    > "$EVIDENCE_DIR/alternative-build.log"
tail -n 2 "$EVIDENCE_DIR/fixture-build.log"

step "Record the artifact identities"
{
    echo "platform                 $PLATFORM"
    echo "host                     $(uname -s) $(uname -m)"
    echo "sil runner image         $SIL_IMAGE"
    echo "sil version              $(sil_tool sil-run --version | tr -d '\r')"
    echo "fixture image id         $(docker image inspect --format '{{.Id}}' "$FIXTURE_IMAGE")"
    echo "proof image id           $(docker image inspect --format '{{.Id}}' "$PROOF_IMAGE")"
    # Two expressions rather than one alternation: BSD sed has no `\|` in a
    # basic regular expression, and this script runs on a developer's Mac as
    # well as in CI.
    sed -n -e 's/^ARG \(EXAMPLES_[A-Z_]*\)=\(.*\)$/\1 \2/p' \
           -e 's/^ARG \(FMPY_VERSION\)=\(.*\)$/\1 \2/p' \
      "$PROOF_DIR/Dockerfile" \
      | awk '{ printf "%-24s %s\n", tolower($1), $2 }'
    echo "participant timeout ms   $PARTICIPANT_TIMEOUT_MS"
} | tee "$EVIDENCE_DIR/identity.txt"

step "Inspect the fixture"
harness /opt/harness/inspect_fixture.py "$NODE_FMU" "$BUS_FMU" \
    --profile /workspace/profile.json \
    | tee "$EVIDENCE_DIR/profile.txt"
cp "$WORKSPACE/profile.json" "$EVIDENCE_DIR/profile.json"

step "Inspect the revision this fixture rejected"
# What that revision claims about the layered standard, which is the one thing
# it does better than the selected one.
harness /opt/harness/inspect_fixture.py "$ALTERNATIVE_NODE_FMU" \
    --profile /workspace/alternative-profile.json \
    | tee "$EVIDENCE_DIR/alternative-profile.txt"

step "Check the fixture digest against its pin"
# Read as JSON rather than scraped: a digest that failed to match because
# the document was formatted differently would be a confusing way to
# report that the fixture changed.
digest="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["fixture_digest"])' \
    "$EVIDENCE_DIR/profile.json")"
if [ "$digest" != "$EXPECTED_FIXTURE_DIGEST" ]; then
    echo "fixture digest $digest does not match the pinned" \
         "$EXPECTED_FIXTURE_DIGEST" >&2
    exit 1
fi
echo "fixture digest $digest"

step "Check the built archives against their pins"
# Checked inside the image, where the archives are: `sha256sum -c` reports
# which archive moved, and the built FMUs are what the rest of this script
# and every downstream issue actually execute.
printf '%s\n' "$EXPECTED_ARCHIVE_DIGESTS" \
    | docker_run --entrypoint sha256sum -i "$FIXTURE_IMAGE" -c - \
    | tee "$EVIDENCE_DIR/archive-digests.txt"

step "Reference exchange: the CAN node under an independent FMI importer"
harness /opt/harness/reference_exchange.py "$NODE_FMU" \
    --trace /workspace/reference-exchange.json \
    --log /workspace/reference-fmu.log \
    | tee "$EVIDENCE_DIR/reference-exchange.txt"
cp "$WORKSPACE/reference-exchange.json" "$WORKSPACE/reference-fmu.log" \
    "$EVIDENCE_DIR/"

step "The same exchange against the upstream revision this fixture rejected"
# Expected to fail, so its exit status is recorded rather than allowed to end
# the script. This is the evidence for the version selection: one importer,
# one expectation, two revisions.
set +e
harness /opt/harness/reference_exchange.py "$ALTERNATIVE_NODE_FMU" \
    --trace /workspace/alternative-exchange.json \
    --log /workspace/alternative-fmu.log \
    > "$WORKSPACE/alternative-exchange.out" 2>&1
alternative_status=$?
set -e
{
    echo "fmu         $ALTERNATIVE_NODE_FMU"
    echo "exit status $alternative_status"
    echo "--- output ---"
    cat "$WORKSPACE/alternative-exchange.out"
} | tee "$EVIDENCE_DIR/alternative-exchange.txt"
# The FMU's own log is the other half of that diagnostic: the exception says
# which call failed, the log says what the FMU objected to.
cp "$WORKSPACE/alternative-fmu.log" "$EVIDENCE_DIR/alternative-fmu.log"
if [ "$alternative_status" -eq 0 ]; then
    echo "the rejected revision now passes the reference exchange; the" \
         "version selection in README.md needs revisiting" >&2
    exit 1
fi

step "Build the Manifests that put current SiL in front of the same FMU"
sil_tool python3 /opt/consumer/manifest.py "$NODE_FMU" /workspace \
    | tee "$EVIDENCE_DIR/manifest-hashes.txt"

step "What the released FMI Importer answers"
# Both Runs are expected to fail, so their exit codes are recorded rather than
# allowed to end the script — and then checked, because the measurement this
# gate publishes is those two exit codes. A release that answers differently
# has moved the gate, and saying so here is cheaper than noticing later that
# README.md describes a Run nobody reproduces.
#
#   1  run failure: the FMU refused to be instantiated without Event Mode
#   2  Manifest error: the Channel names variables the importer cannot see
expect_no_channels=1
expect_binary_channel=2
for variant in no-channels binary-channel; do
    set +e
    sil_run "/workspace/$variant.json" -o "/workspace/$variant.mcap" \
        --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS" \
        > "$WORKSPACE/$variant.out" 2>&1
    status=$?
    set -e
    {
        echo "manifest    $variant.json"
        echo "exit status $status"
        echo "--- output ---"
        cat "$WORKSPACE/$variant.out"
    } | tee "$EVIDENCE_DIR/sil-$variant.txt"
    expected="expect_${variant//-/_}"
    if [ "$status" != "${!expected}" ]; then
        echo "$variant exited $status, and this fixture is written against" \
             "${!expected}" >&2
        exit 1
    fi
done

step "What the kernel says about the Channel the importer could not fill"
# The bounded CAN frame Channel is declared in the same Manifest the importer
# rejected. The footprint report reads it without running anything, which is
# what separates a missing Importer capability from a missing Channel one.
sil_tool sil-footprint /workspace/binary-channel.json \
    | tee "$EVIDENCE_DIR/footprint.txt"

step "Done"
echo "evidence  $EVIDENCE_DIR"
echo "workspace $WORKSPACE"
