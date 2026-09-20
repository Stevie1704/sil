#!/usr/bin/env bash
# Validate the updated determinism checker against the #117 esmini consumer.
#
# The v0.1.0 proof beside this script is release evidence and stays as it is:
# its `sil-check` invocation carries no Process-participant deadline, because
# the released checker has none. This script answers the separate question
# issue #134 asks — does the *unreleased* checker hand the same deadline to
# both of the Runs it compares, on the same consumer artifact?
#
#   ./validate-checker-deadline.sh [evidence-dir] [workspace-dir]
#
# The checker under test is mounted read-only as a single file and run as a
# script; nothing is copied into the proof image and no SiL checkout reaches
# the runner, the participants, or the Manifest builder, all of which stay the
# pinned released ones. Requires docker and, for the image build, network
# access. Replace this with the released reproduction in run-proof.sh once a
# release containing the fix exists.

set -euo pipefail

PROOF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PROOF_DIR/../.." && pwd)"
EVIDENCE_DIR="${1:-$PROOF_DIR/evidence-checker-deadline}"
WORKSPACE="${2:-$(mktemp -d)}"

IMAGE="${SIL_ESMINI_IMAGE:-sil-esmini-proof:local}"
PLATFORM="${SIL_ESMINI_PLATFORM:-linux/amd64}"
PARTICIPANT_TIMEOUT_MS="${SIL_ESMINI_PARTICIPANT_TIMEOUT_MS:-30000}"

SIL_IMAGE="$(sed -n 's/^FROM \(ghcr\.io[^ ]*\) AS consumer$/\1/p' "$PROOF_DIR/Dockerfile")"
CHECKER="$REPO_ROOT/python/src/sil/check.py"

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

sil_run() { docker_run "$IMAGE" "$@"; }
sil_tool() { local tool="$1"; shift; docker_run --entrypoint "$tool" "$IMAGE" "$@"; }
# The checker under test, and only it: the image's own `sil` installation
# still serves the Step endpoint and the Manifest builder.
sil_check_under_test() {
    docker_run --mount "type=bind,src=$CHECKER,dst=/checker/check.py,readonly" \
        --entrypoint python3 "$IMAGE" /checker/check.py "$@"
}

step() { printf '\n=== %s ===\n' "$1"; }

step "Build the derived consumer image"
docker build --platform "$PLATFORM" --tag "$IMAGE" "$PROOF_DIR"

step "Record the identities this validation runs on"
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
    echo "checker source           python/src/sil/check.py (unreleased)"
    echo "checker sha256           $(sha256_of "$CHECKER")"
    echo "checker revision         $(git -C "$REPO_ROOT" rev-parse HEAD)"
    echo "checker worktree         $(git -C "$REPO_ROOT" status --porcelain -- python/src/sil/check.py | wc -l | tr -d ' ') local modifications"
} | tee "$EVIDENCE_DIR/identity.txt"

step "Build the nominal Manifest with the released builder"
nominal_hash="$(sil_tool python3 /opt/consumer/manifest.py /workspace/alks-cut-in.json)"
echo "nominal  $nominal_hash" | tee "$EVIDENCE_DIR/manifest-hashes.txt"
released_hash="$(awk '$1 == "nominal" { print $2 }' "$PROOF_DIR/evidence/manifest-hashes.txt")"
if [ "$nominal_hash" != "$released_hash" ]; then
    echo "this is no longer the Manifest the release proof ran" >&2
    exit 1
fi

step "Check determinism with a forwarded response deadline"
# The subject of this run: one command, both Runs deadline-bounded.
checker_output="$(sil_check_under_test /workspace/alks-cut-in.json \
    --runner sil-run --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS")"
echo "$checker_output" | tee "$EVIDENCE_DIR/determinism.txt"
checked_digest="${checker_output##deterministic: }"
if [ "$checked_digest" = "$checker_output" ]; then
    echo "the checker did not report two agreeing Recordings" >&2
    exit 1
fi

step "Retain one deadline-bounded Recording and verify the KPI against it"
# The checker compares its two Recordings and discards them. This Run is the
# same command with the same deadline, kept so the post-hoc checks have bytes
# to read and so the digest the checker reported can be tied to an artifact.
sil_run /workspace/alks-cut-in.json \
    --participant-timeout-ms "$PARTICIPANT_TIMEOUT_MS" -o /workspace/run-1.mcap
retained_digest="$(sha256_of "$WORKSPACE/run-1.mcap")"
release_digest="$(awk '$1 == "run-1.mcap" { print $2 }' "$PROOF_DIR/evidence/determinism.txt")"
{
    echo "checked      $checked_digest"
    echo "retained     $retained_digest"
    echo "v0.1.0 proof $release_digest"
    echo "bytes        $(wc -c <"$WORKSPACE/run-1.mcap" | tr -d ' ')"
} | tee "$EVIDENCE_DIR/recording-hashes.txt"
if [ "$checked_digest" != "$retained_digest" ]; then
    echo "the checked Runs and the retained Run disagree" >&2
    exit 1
fi

sil_tool python3 /opt/consumer/verify.py \
    --recording /workspace/run-1.mcap \
    --manifest /workspace/alks-cut-in.json \
    --reference /opt/consumer/reference/alks_r157_expected.json \
    --observations /workspace/observations.json
cp "$WORKSPACE/observations.json" "$EVIDENCE_DIR/observations.json"

printf '\nchecker validation complete; evidence is in %s\n' "$EVIDENCE_DIR"
