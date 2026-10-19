#!/usr/bin/env bash
# Native Linux setup of the profiling proof (issue #314), for a GitHub-hosted
# ubuntu-24.04 runner or the same distribution.
#
#     native-setup.sh PREFIX
#
# installs perf (the linux-tools package of the running kernel) and the
# pinned Heaptrack with apt, builds and installs sil-run and the Clock shim
# into PREFIX/native, and makes a Python environment with SiL in
# PREFIX/python. It needs sudo for apt only. It does not change
# kernel.perf_event_paranoid; the workflow does that as a separate, disclosed
# step.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
prefix=$1
# Heaptrack is pinned to the Ubuntu 24.04 package. perf must match the
# running kernel, so its package version is the kernel's; the proof records
# it.
heaptrack_version=${SIL_PROFILING_HEAPTRACK_VERSION:-}
sudo apt-get update
heaptrack_package=heaptrack${heaptrack_version:+=$heaptrack_version}
sudo apt-get install -y --no-install-recommends \
    "linux-tools-$(uname -r)" linux-tools-common "$heaptrack_package" \
    binutils build-essential cmake
cmake -S "$root" -B "$prefix/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$prefix/build" --target sil-run sil-run-diagnostics \
    sil-diagnostics-launcher sil_clock_shim -j "$(nproc)"
cmake --install "$prefix/build" --prefix "$prefix/native"
python3 -m venv "$prefix/python"
"$prefix/python/bin/pip" install --no-cache-dir \
    --requirement "$root/container/requirements.lock" "$root/python"
