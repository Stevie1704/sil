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
# a result rather than an accident. The two compile-time definitions below are
# what keeps the selected revision on the loadable side of that line.

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
git remote add origin "$EXAMPLES_REPOSITORY" 2>/dev/null || true
git fetch -q --depth 1 origin "$REVISION"
git checkout -q FETCH_HEAD
test "$(git rev-parse HEAD)" = "$REVISION"
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

# Compile each source-code FMU for this platform and pack it back up. The
# archive is written with one fixed timestamp for every member, so an FMU's
# own bytes depend on its contents and not on when it was built.
python - "$OUTPUT" <<'PY'
import ctypes
import os
import sys
import zipfile
from pathlib import Path

from fmpy import extract
from fmpy.build import build_platform_binary

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

# Two compile-time definitions, and no edit to any upstream file. Both are
# what this machine class needs to turn upstream's sources into a loadable
# shared-object FMU; neither changes what the FMU does.
#
# `FMU_IDENTIFIER_H` is the include guard of upstream's `FmuIdentifier.h`,
# which defines FMI3_FUNCTION_PREFIX unconditionally and would export
# `DemoCanNodeTriggeredOutput_fmi3InstantiateCoSimulation` instead of
# `fmi3InstantiateCoSimulation`. `fmi3Functions.h` reserves that prefix for
# source and static-library distribution: "For FMUs compiled in a
# DLL/sharedObject, the 'actual' function names are used and
# 'FMI3_FUNCTION_PREFIX' must not be defined." Defining the guard makes that
# header a no-op, which is what a shared object needs.
#
# `_strdup` is MSVC's spelling of POSIX `strdup`. Upstream's `Fmu.c` calls it
# unconditionally, and a C compiler that is not MSVC links a shared object
# with `_strdup` undefined — it builds, and then cannot be loaded at all. The
# definition maps the one call onto the standard function.
COMPILE_DEFINITIONS = {
    "FMI_DEFINITIONS:STRING": "FMU_IDENTIFIER_H;_strdup=strdup"
}


def repack(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            member = zipfile.ZipInfo(
                str(path.relative_to(source)), date_time=FIXED_TIMESTAMP
            )
            member.compress_type = zipfile.ZIP_DEFLATED
            executable = os.access(path, os.X_OK)
            member.external_attr = (0o755 if executable else 0o644) << 16
            archive.writestr(member, path.read_bytes())


for fmu in sorted(Path(sys.argv[1]).glob("*.fmu")):
    print(f"=== compiling {fmu.name} ===", flush=True)
    extracted = Path(extract(fmu))
    build_platform_binary(extracted,
                          cmake_options=dict(COMPILE_DEFINITIONS))
    binaries = sorted(
        str(path.relative_to(extracted))
        for path in (extracted / "binaries").rglob("*") if path.is_file()
    )
    if not binaries:
        raise SystemExit(f"{fmu.name} carries no compiled binary")
    # Load it with every symbol resolved, and resolve one entry point. A
    # shared object links even with an undefined symbol in it, and a prefixed
    # export satisfies no importer, so "the compiler said nothing" is not the
    # same as "this FMU can be driven".
    library = ctypes.CDLL(str(extracted / binaries[0]), mode=os.RTLD_NOW)
    library.fmi3InstantiateCoSimulation
    repack(extracted, fmu)
    print(f"{fmu.name} {' '.join(binaries)}")
PY
