#!/bin/sh
# One-command demonstration of the ADAS reference application through the
# Native participant ABI, using only installed SiL interfaces:
#
#   - sil-run and silschema from an installed prefix, and its include/sil;
#   - sil-compare and python3 with the sil wheel;
#   - a C compiler (cc, or $CC).
#
# Usage: run.sh WORKDIR
#
# It generates the Schema layouts, builds the library and its wrong-sign
# variant, prepares the maneuver Recordings, runs the Manifest twice, requires
# identical Recording bytes, compares each maneuver with its enumerated
# expected trajectory, and requires the wrong-sign build to fail the hazard
# comparison. It exits non-zero on the first step that does not hold.
set -eu

if [ $# -ne 1 ]; then
    echo "usage: $0 WORKDIR" >&2
    exit 2
fi
here=$(cd "$(dirname "$0")" && pwd)
work=$1
mkdir -p "$work"
work=$(cd "$work" && pwd)
prefix=$(cd "$(dirname "$(command -v sil-run)")/.." && pwd)
cc=${CC:-cc}
maneuvers="clear hazard release unavailable boundaries"

silschema "$here/schemas.json" "$work/include/adas_messages.h"
build() {
    # $1: output library, remaining: extra compiler flags
    out=$1
    shift
    "$cc" -std=c11 -O2 -Wall -Wextra -shared -fPIC "$@" \
        -I"$prefix/include" -I"$here" -I"$work/include" \
        -o "$out" "$here/adas_reference.c" "$here/sil_adapter.c" -lm
}
build "$work/adas_reference.so"
build "$work/adas_reference_wrong_sign.so" -DADAS_REFERENCE_WRONG_SIGN

python3 "$here/prepare.py" "$work"
python3 "$here/manifest.py" "$work/reference.json" \
    --inputs "$work" --library "$work/adas_reference.so"
sil-run "$work/reference.json" -o "$work/run-1.mcap"
sil-run "$work/reference.json" -o "$work/run-2.mcap"
cmp "$work/run-1.mcap" "$work/run-2.mcap"
for maneuver in $maneuvers; do
    sil-compare "$work/$maneuver.contract.json" "$work/run-1.mcap" \
        "$work/$maneuver.expected.mcap" > "$work/$maneuver.compare.txt"
    echo "$maneuver: pass"
done

python3 "$here/manifest.py" "$work/wrong-sign.json" \
    --inputs "$work" --library "$work/adas_reference_wrong_sign.so"
sil-run "$work/wrong-sign.json" -o "$work/wrong-sign.mcap"
status=0
sil-compare "$work/hazard.contract.json" "$work/wrong-sign.mcap" \
    "$work/hazard.expected.mcap" > "$work/wrong-sign.compare.txt" || status=$?
if [ "$status" -ne 1 ]; then
    echo "the wrong-sign build did not fail the hazard comparison" \
         "(exit $status)" >&2
    exit 1
fi
echo "wrong sign: fails as required"
