#!/usr/bin/env bash
# GitHub Actions ubuntu-24.04; package installation is in the workflow.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:?give a fresh evidence directory}
work=${2:?give a fresh build directory}
mkdir -p "$work"
cmake -S "$root" -B "$work/gcc" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=gcc-12 -DCMAKE_CXX_COMPILER=g++-12
cmake --build "$work/gcc" --target sil-run sil-run-diagnostics -j2
# This is a proof-only executable link. The installed runner is untouched.
# The executable owns the XRay runtime; selected sleds reside in a dlopen DSO.
cmake -S "$root" -B "$work/xray" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18 \
  "-DCMAKE_PROJECT_INCLUDE=$root/proofs/function-tracing/xray-bridge.cmake" \
  '-DCMAKE_EXE_LINKER_FLAGS=-fxray-instrument -Wl,--export-dynamic'
cmake --build "$work/xray" --target sil-run-diagnostics -j2
PYTHONPATH="$root/python/src" "$root/.venv/bin/python" \
  "$root/proofs/function-tracing/proof.py" "$evidence" \
  --gcc-build "$work/gcc" --xray-build "$work/xray"
