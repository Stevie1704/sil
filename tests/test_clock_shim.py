"""Isolated unit tests for the virtual clock shim (issue #26).

The shim is preloaded into a plain probe process against a *test-controlled*
time region — no kernel, no manifest. The test creates the region as a small
memory-mapped file (exactly what the shim maps), writes a chosen ``t``/``epoch``,
runs the probe under the platform preload, and asserts the probe's printed
observations. Any shim regression localizes to this file.
"""

import mmap
import os
import struct
import subprocess
import sys

import pytest

# Region layout mirrors include/sil/clock_region.h: two little-endian u64.
REGION_FMT = "<QQ"
REGION_SIZE = struct.calcsize(REGION_FMT)
REGION_ENV = "SIL_CLOCK_REGION"


@pytest.fixture
def clock_region(tmp_path):
    """Create a shared time region file; yield a writer callback + its path.

    The file is mmap'd MAP_SHARED so writes are visible to the preloaded shim
    in the child probe process, modelling the kernel advancing time per step.
    """
    path = tmp_path / "clock_region.bin"
    path.write_bytes(b"\x00" * REGION_SIZE)
    fd = os.open(path, os.O_RDWR)
    region = mmap.mmap(fd, REGION_SIZE, mmap.MAP_SHARED,
                       mmap.PROT_READ | mmap.PROT_WRITE)
    os.close(fd)

    def write(t: int, epoch: int) -> None:
        region.seek(0)
        region.write(struct.pack(REGION_FMT, t, epoch))
        region.flush()

    try:
        yield str(path), write
    finally:
        region.close()


@pytest.fixture(scope="session")
def probe(build_dir):
    exe = build_dir / "sil_clock_probe"
    assert exe.exists(), f"clock probe not built at {exe}"
    return exe


@pytest.fixture(scope="session")
def shim(build_dir):
    for candidate in ("libsil_clock_shim.dylib", "libsil_clock_shim.so",
                      "sil_clock_shim.dylib", "sil_clock_shim.so"):
        p = build_dir / candidate
        if p.exists():
            return p
    pytest.fail(f"clock shim library not built in {build_dir}")


def run_probe(probe, shim, region_name):
    """Run the probe under the preload, return parsed key=value output."""
    env = dict(os.environ)
    env[REGION_ENV] = region_name
    if sys.platform == "darwin":
        env["DYLD_INSERT_LIBRARIES"] = str(shim)
    else:
        env["LD_PRELOAD"] = str(shim)
    proc = subprocess.run([str(probe)], capture_output=True, text=True, env=env,
                          timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


T = 12_345_000  # 12.345 ms of virtual time
EPOCH = 1_700_000_000_000_000_000  # a fixed realtime epoch, ns


def test_monotonic_ids_return_virtual_t(probe, shim, clock_region):
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert int(out["monotonic"]) == T
    assert int(out["monotonic_raw"]) == T


def test_realtime_id_returns_epoch_plus_t(probe, shim, clock_region):
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert int(out["realtime"]) == EPOCH + T


def test_getres_reports_one_nanosecond(probe, shim, clock_region):
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert int(out["getres"]) == 1


def test_gettimeofday_and_time_use_realtime(probe, shim, clock_region):
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    # gettimeofday is microsecond-truncated; time is second-truncated.
    total = EPOCH + T
    assert int(out["gettimeofday"]) == (total // 1000) * 1000
    assert int(out["time"]) == (total // 1_000_000_000) * 1_000_000_000


def test_reads_are_frozen_within_a_step(probe, shim, clock_region):
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert out["frozen"] == "1"


def test_sleeps_return_success_immediately(probe, shim, clock_region):
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert out["nanosleep"] == "0"
    assert out["usleep"] == "0"
    assert out["sleep"] == "0"
    if sys.platform.startswith("linux"):
        assert out["clock_nanosleep"] == "0"
    # Time did not advance across the sleep family.
    assert int(out["after_sleep"]) == T


def test_frozen_across_steps(probe, shim, clock_region):
    """Advancing the region between reads changes t; within a read it is fixed."""
    name, write = clock_region
    write(T, EPOCH)
    first = run_probe(probe, shim, name)
    write(T * 2, EPOCH)
    second = run_probe(probe, shim, name)
    assert int(first["monotonic"]) == T
    assert int(second["monotonic"]) == T * 2
