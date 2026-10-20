#!/usr/bin/env bash
# Qualification belongs on the workflow's Linux x86-64/glibc host.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:?give a fresh evidence directory}
work=${2:?give a fresh build directory}
cmake -S "$root" -B "$work" -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_C_COMPILER=gcc-12 -DCMAKE_CXX_COMPILER=g++-12 \
  '-DCMAKE_C_FLAGS_RELWITHDEBINFO=-O2 -g -DNDEBUG' \
  '-DCMAKE_CXX_FLAGS_RELWITHDEBINFO=-O2 -g -DNDEBUG'
cmake --build "$work" --target sil-run sil-run-diagnostics sil_clock_shim -j2
PYTHONPATH="$root/python/src" "$root/.venv/bin/python" \
  "$root/proofs/source-debugging/proof.py" "$evidence" --build "$work"
