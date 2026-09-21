#!/usr/bin/env bash
# Build the CAN acceptance fixture from one pinned upstream revision.
#
#   build-fixture.sh <examples-revision> <output-directory>
#
# Upstream publishes no release, no tag, and no built artifact: the demo FMUs
# exist as sources plus a packaging script, and the packaged FMU is a
# source-code FMU with no binary in it. So this does all three steps —
# check out the pinned revision, pack each demo, compile the platform binary —
# and every input it uses is pinned: the examples revision is a commit SHA,
# the FMI-LS-BUS headers are fetched by upstream's own script from the commit
# that script names, and the toolchain is the image this runs in.
#
# It is run at image build time, where the network is available. Everything
# afterwards runs against the built fixture with no network at all.
#
# It exits non-zero when a revision does not produce a loadable FMU, which is
# a result rather than an accident. The two compile-time definitions that keep
# the selected revision on the loadable side of that line are in
# `pack_fixture.py`, with the reason for each.

set -euo pipefail

REVISION="${1:?usage: build-fixture.sh <examples-revision> <output-directory>}"
OUTPUT="${2:?usage: build-fixture.sh <examples-revision> <output-directory>}"

EXAMPLES_REPOSITORY=https://github.com/modelica/fmi-ls-bus-examples.git

# The two CAN demos. The node is what the milestone drives; the bus simulation
# FMU is what a later issue connects two nodes through, and its description is
# part of what this fixture publishes.
DEMOS=(can-node-triggered-output can-bus-simulation)

# Upstream's notice file, taken from the merged revision that carries it. On
# `main` the packaging script finds it in the checkout; on the pull-request
# revision this proof also builds, that script resolves it four directories
# above itself — a path that predates the fix merged into `main` — so the file
# is placed where that script looks rather than the script being edited. This
# touches packaging only: no FMU source, description, or header is changed.
LICENSE_REVISION=cc42cacd26c7f5edbb20959b0e0c56922c2f0cc2
LICENSE_SHA256=71bc00b50a908efc960aab506d523d4ac81123089ed124f7a442cd1106bbdd44

WORKSPACE=/fixture-build
CHECKOUT="$WORKSPACE/src/upstream"

mkdir -p "$CHECKOUT" "$OUTPUT"
cd "$CHECKOUT"

git init -q .
git remote add origin "$EXAMPLES_REPOSITORY"
git fetch -q --depth 1 origin "$REVISION"
git checkout -q FETCH_HEAD
# A revision is fetched by SHA, and what arrives is checked rather than
# assumed: a pin that silently resolved to something else would take the whole
# fixture with it.
checked_out="$(git rev-parse HEAD)"
if [ "$checked_out" != "$REVISION" ]; then
    echo "checked out $checked_out, and the pinned revision is $REVISION" >&2
    exit 1
fi
echo "examples revision $REVISION"

# Fetched with the same library upstream's own packaging script fetches the
# FMI-LS-BUS headers with, so the fixture build needs no tool the packaging
# step does not already need.
python - "$LICENSE_REVISION" "$WORKSPACE/LICENSE.txt" <<'PY'
import sys
import urllib.request
from pathlib import Path

revision, destination = sys.argv[1], Path(sys.argv[2])
url = ("https://raw.githubusercontent.com/modelica/fmi-ls-bus-examples/"
       f"{revision}/LICENSE.txt")
with urllib.request.urlopen(url) as response:
    destination.write_bytes(response.read())
PY
echo "${LICENSE_SHA256}  $WORKSPACE/LICENSE.txt" | sha256sum -c -

for demo in "${DEMOS[@]}"; do
    echo "=== packing $demo ==="
    ( cd "$demo" && python PackFmu.py )
    mv "$demo"/*.fmu "$OUTPUT/"
done

# Compile each source-code FMU for this platform and pack it back up. What
# that takes, and why, is in `pack_fixture.py` beside this script.
python /usr/local/bin/pack_fixture.py "$OUTPUT"
