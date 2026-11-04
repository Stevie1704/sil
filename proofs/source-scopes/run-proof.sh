#!/usr/bin/env bash
# GitHub Actions ubuntu-24.04; package installation is in
# .github/workflows/source-scopes.yml. Builds SiL with the pinned GCC 12,
# then runs the ADAS qualification (Q1, Q20, Q21).
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
evidence=${1:?give a fresh evidence directory}
build=${2:?give a fresh build directory}
cmake -S "$root" -B "$build" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=gcc-12 -DCMAKE_CXX_COMPILER=g++-12
cmake --build "$build" -j2 --target sil-run sil-run-diagnostics \
  sil-diagnostics-launcher sil_source_scope
"$root/.venv/bin/python" "$root/proofs/source-scopes/proof.py" "$evidence" \
  --build "$build"
