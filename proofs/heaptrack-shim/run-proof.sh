#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:?give a fresh evidence directory}
work=${2:?give a build directory}
cmake -S "$root" -B "$work" -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build "$work" --target sil-run sil-run-diagnostics sil_clock_shim -j2
PYTHONPATH="$root/python/src" "${SILPROOF_PYTHON:-$root/.venv/bin/python}" \
  "$root/proofs/heaptrack-shim/proof.py" "$evidence" --build "$work" "${@:3}"
