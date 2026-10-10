#!/bin/sh
# One-command demonstration of the ADAS reference application through the
# Native participant ABI, using only installed SiL interfaces:
#
#   - sil-run and silschema from an installed prefix, and its include/sil;
#   - sil compare and python3 with the sil wheel;
#   - a C compiler (cc, or $CC).
#
# Usage: run.sh WORKDIR
#
# It generates the Schema layouts, builds the library and its wrong-sign and
# arrival-time variants, prepares the maneuver Recordings, runs the reference
# Manifest and each experiment Manifest twice, requires identical Recording
# bytes, compares each maneuver and experiment with its enumerated expected
# trajectory, and requires the wrong-sign build to fail the hazard and
# ordering comparisons and the arrival-time build to fail the delay
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

silschema "$here/schemas.json" "$work/include/adas_messages.h"
build() {
    # $1: output library, remaining: extra compiler flags
    out=$1
    shift
    "$cc" -std=c11 -O2 -ffp-contract=off -Wall -Wextra -shared -fPIC "$@" \
        -I"$prefix/include" -I"$here" -I"$work/include" \
        -o "$out" "$here/adas_reference.c" "$here/sil_adapter.c" -lm
}
build "$work/adas_reference.so"
build "$work/adas_reference_wrong_sign.so" -DADAS_REFERENCE_WRONG_SIGN
build "$work/adas_reference_arrival_time.so" -DADAS_REFERENCE_ARRIVAL_TIME

# run_twice NAME: runs $work/NAME.json twice and requires identical bytes.
run_twice() {
    sil-run "$work/$1.json" -o "$work/$1-1.mcap"
    sil-run "$work/$1.json" -o "$work/$1-2.mcap"
    cmp "$work/$1-1.mcap" "$work/$1-2.mcap"
}

python3 "$here/prepare.py" "$work"
python3 "$here/manifest.py" "$work/reference.json" \
    --inputs "$work" --library "$work/adas_reference.so"
run_twice reference
for expected in "$here"/maneuvers/*.expected.csv; do
    name=$(basename "$expected" .expected.csv)
    maneuver=${name%%.*}
    experiment=${name#"$maneuver"}
    experiment=${experiment#.}
    run=reference
    if [ -n "$experiment" ]; then
        run=$name
        python3 "$here/manifest.py" "$work/$run.json" --inputs "$work" \
            --library "$work/adas_reference.so" --experiment "$experiment"
        run_twice "$run"
    fi
    sil compare "$work/$maneuver.contract.json" "$work/$run-1.mcap" \
        "$work/$name.expected.mcap" > "$work/$name.compare.txt"
    echo "$name: pass"
done

# fails_comparison NAME EXPECTATION: requires sil compare to report a
# divergence (exit 1) between $work/NAME.mcap and the expectation.
fails_comparison() {
    status=0
    sil compare "$work/${2%%.*}.contract.json" "$work/$1.mcap" \
        "$work/$2.expected.mcap" > "$work/$1.$2.compare.txt" || status=$?
    if [ "$status" -ne 1 ]; then
        echo "$1 did not fail the $2 comparison (exit $status)" >&2
        exit 1
    fi
}

python3 "$here/manifest.py" "$work/wrong-sign.json" \
    --inputs "$work" --library "$work/adas_reference_wrong_sign.so"
sil-run "$work/wrong-sign.json" -o "$work/wrong-sign.mcap"
fails_comparison wrong-sign hazard
fails_comparison wrong-sign ordering
echo "wrong sign: fails as required"

python3 "$here/manifest.py" "$work/arrival-time.json" --inputs "$work" \
    --library "$work/adas_reference_arrival_time.so" --experiment delay
sil-run "$work/arrival-time.json" -o "$work/arrival-time.mcap"
fails_comparison arrival-time cadence.delay
echo "arrival time: fails as required"
