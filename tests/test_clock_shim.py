"""Isolated unit tests for the virtual clock shim (issue #26).

The shim is preloaded into a plain probe process against a *test-controlled*
time region — no kernel, no manifest. The test creates the region as a small
memory-mapped file (exactly what the shim maps), writes a chosen ``t``/``epoch``,
runs the probe under the platform preload, and asserts the probe's printed
observations. Any shim regression localizes to this file.
"""

import errno
import mmap
import os
import struct
import subprocess
import sys

import pytest

# Region layout mirrors include/sil/clock_region.h: three little-endian u64.
REGION_FMT = "<QQQ"
REGION_SIZE = struct.calcsize(REGION_FMT)
REGION_ENV = "SIL_CLOCK_REGION"

# sil_sleep_policy values, mirroring the enum in the same header.
IMMEDIATE = 0
REJECT = 1


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

    def write(t: int, epoch: int, sleep_policy: int = IMMEDIATE) -> None:
        region.seek(0)
        region.write(struct.pack(REGION_FMT, t, epoch, sleep_policy))
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


def run_probe(probe, shim, region_name=None):
    """Run the probe under the preload, return parsed key=value output.

    ``region_name`` of None leaves the region unset, which is the shim's
    region-absent fallback: every interposer defers to the real libc. That is
    the oracle for what a pass-through clock ID must answer.
    """
    env = dict(os.environ)
    if region_name is None:
        env.pop(REGION_ENV, None)
    else:
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
    """Immediate policy: the recorded pre-#52 behavior, unchanged."""
    name, write = clock_region
    write(T, EPOCH, IMMEDIATE)
    out = run_probe(probe, shim, name)
    assert out["nanosleep"] == "0"
    assert out["usleep"] == "0"
    assert out["sleep"] == "0"
    if sys.platform.startswith("linux"):
        assert out["clock_nanosleep"] == "0"
    # Time did not advance across the sleep family.
    assert int(out["after_sleep"]) == T


def test_absent_policy_field_means_immediate(probe, shim, clock_region):
    """A zeroed region is what a writer that predates #52 leaves behind."""
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert out["nanosleep"] == "0"
    assert out["sleep"] == "0"


def test_unrecognized_policy_reads_as_immediate(probe, shim, clock_region):
    """A reader never fails closed on a value a newer writer might add."""
    name, write = clock_region
    write(T, EPOCH, 99)
    out = run_probe(probe, shim, name)
    assert out["nanosleep"] == "0"
    assert out["sleep"] == "0"


def test_immediate_reports_the_whole_duration_as_elapsed(probe, shim,
                                                         clock_region):
    name, write = clock_region
    write(T, EPOCH, IMMEDIATE)
    out = run_probe(probe, shim, name)
    assert int(out["nanosleep_rem"]) == 0


def test_reject_fails_the_sleep_family_with_enosys(probe, shim, clock_region):
    name, write = clock_region
    write(T, EPOCH, REJECT)
    out = run_probe(probe, shim, name)

    assert out["nanosleep"] == "-1"
    assert int(out["nanosleep_errno"]) == errno.ENOSYS
    assert out["usleep"] == "-1"
    assert int(out["usleep_errno"]) == errno.ENOSYS

    # Reject must not block either: virtual time is still frozen.
    assert int(out["after_sleep"]) == T


def test_reject_reports_the_whole_duration_as_remaining(probe, shim,
                                                        clock_region):
    """Nothing elapsed, so `rem` is the full request, not zero."""
    name, write = clock_region
    write(T, EPOCH, REJECT)
    out = run_probe(probe, shim, name)
    assert int(out["nanosleep_rem"]) == 1_500_000_000


def test_reject_cannot_fail_sleep_so_it_reports_seconds_unslept(
        probe, shim, clock_region):
    """POSIX gives sleep() no error return; the unslept count is the signal."""
    name, write = clock_region
    write(T, EPOCH, REJECT)
    out = run_probe(probe, shim, name)
    assert out["sleep"] == "2"
    assert int(out["sleep_errno"]) == errno.ENOSYS


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="macOS libc has no clock_nanosleep to interpose")
def test_reject_returns_enosys_from_clock_nanosleep_without_errno(
        probe, shim, clock_region):
    """clock_nanosleep returns the error number and leaves errno alone."""
    name, write = clock_region
    write(T, EPOCH, REJECT)
    out = run_probe(probe, shim, name)
    assert int(out["clock_nanosleep"]) == errno.ENOSYS
    assert int(out["clock_nanosleep_errno"]) == 0
    assert int(out["clock_nanosleep_rem"]) == 1_500_000_000


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="macOS libc has no clock_nanosleep to interpose")
@pytest.mark.parametrize("policy", [IMMEDIATE, REJECT])
def test_absolute_clock_nanosleep_leaves_remaining_untouched(
        probe, shim, clock_region, policy):
    """POSIX ignores `rem` for an absolute sleep, so the shim must not write it.

    The probe seeds `rem` with 7 s; either policy overwriting it fails here.
    """
    name, write = clock_region
    write(T, EPOCH, policy)
    out = run_probe(probe, shim, name)
    expected = errno.ENOSYS if policy == REJECT else 0
    assert int(out["clock_nanosleep_abs"]) == expected
    assert int(out["clock_nanosleep_abs_rem"]) == 7_000_000_000


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="macOS libc has no clock_nanosleep to interpose")
def test_cpu_time_clock_sleeps_pass_through(probe, shim, clock_region):
    """A CPU-time clock is not virtualized, so the policy cannot reach it.

    Asserted as "both policies agree" rather than pinning a return value: the
    point is that the shim steps aside, and whatever the real libc answers is
    then the same under either policy. Pinning the value would test libc.

    The probe uses CLOCK_THREAD_CPUTIME_ID because it returns immediately;
    sleeping on a process CPU clock never returns, since a process blocked in
    the call accrues no CPU time.
    """
    name, write = clock_region
    write(T, EPOCH, IMMEDIATE)
    immediate = run_probe(probe, shim, name)["clock_nanosleep_cpu"]
    write(T, EPOCH, REJECT)
    reject = run_probe(probe, shim, name)["clock_nanosleep_cpu"]
    assert immediate == reject
    # Reject reached the virtualized calls in the same run, so the region was
    # read; this is pass-through, not a policy that failed to apply.
    assert str(errno.ENOSYS) != reject


@pytest.mark.parametrize("policy,expected_iterations", [(IMMEDIATE, 1000),
                                                        (REJECT, 1)])
def test_retry_loop_spins_under_immediate_and_exits_under_reject(
        probe, shim, clock_region, policy, expected_iterations):
    """The point of the reject policy (#52).

    A caller that retries a sleep until a deadline passes cannot make progress
    while virtual time is frozen. Under `immediate` every call succeeds and the
    loop runs to its cap; under `reject` the first call fails and it leaves.
    """
    name, write = clock_region
    write(T, EPOCH, policy)
    out = run_probe(probe, shim, name)
    assert int(out["retry_iterations"]) == expected_iterations
    assert out["retry_capped"] == ("1" if policy == IMMEDIATE else "0")


# --- clock ID classification (issue #76) -------------------------------------
# The shim serves monotonic-class and realtime-class IDs from the region and
# passes every other ID — CPU-time IDs and any unknown or future one — through
# to the real libc.


@pytest.mark.parametrize("key", ["process_cpu", "thread_cpu"])
def test_cpu_time_clocks_pass_through(probe, shim, clock_region, key):
    """A CPU-time clock counts work done, so it advances inside a frozen step.

    This is the property no virtualized clock has: the region's ``t`` does not
    move during a step, so a virtualized read would report the same value both
    times. The probe does a fixed amount of work between the two reads.
    """
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    if out.get(key) == "unavailable":
        pytest.skip(f"{key} clock ID not available on this platform")
    assert out.get(key) != "error", f"{key} read failed"
    assert int(out[f"{key}_after"]) > int(out[f"{key}_before"])


def test_cpu_time_clock_is_not_the_virtual_clock(probe, shim, clock_region):
    """Pinning the old defect: the CPU clock read was epoch + t."""
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    if out.get("process_cpu") == "unavailable":
        pytest.skip("CLOCK_PROCESS_CPUTIME_ID not available on this platform")
    assert int(out["process_cpu_before"]) != EPOCH + T


def test_getres_for_a_cpu_time_clock_matches_the_real_libc(probe, shim,
                                                           clock_region):
    """clock_getres passes CPU-time IDs through rather than reporting 1 ns.

    The oracle is the same probe run with no region, where every interposer
    falls back to libc. Where libc itself answers 1 ns the two values agree
    either way, so this assertion only discriminates on platforms whose CPU
    clocks report a coarser resolution.
    """
    name, write = clock_region
    write(T, EPOCH)
    shimmed = run_probe(probe, shim, name)
    if shimmed["getres_cpu"] == "unavailable":
        pytest.skip("CLOCK_PROCESS_CPUTIME_ID not available on this platform")
    real = run_probe(probe, shim)
    assert shimmed["getres_cpu"] == real["getres_cpu"]
    # The virtualized IDs still report 1 ns.
    assert int(shimmed["getres"]) == 1


def test_unknown_clock_id_passes_through(probe, shim, clock_region):
    """An ID the shim does not classify reaches libc, which rejects it.

    Answering it from the region would invent a class for a clock whose
    meaning the shim cannot know.
    """
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert out["unknown"] == "error"


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="the COARSE clock IDs are Linux-only")
def test_coarse_ids_keep_their_class(probe, shim, clock_region):
    """COARSE variants are the same class as the clocks they approximate."""
    name, write = clock_region
    write(T, EPOCH)
    out = run_probe(probe, shim, name)
    assert int(out["realtime_coarse"]) == EPOCH + T
    assert int(out["monotonic_coarse"]) == T


def test_frozen_across_steps(probe, shim, clock_region):
    """Advancing the region between reads changes t; within a read it is fixed."""
    name, write = clock_region
    write(T, EPOCH)
    first = run_probe(probe, shim, name)
    write(T * 2, EPOCH)
    second = run_probe(probe, shim, name)
    assert int(first["monotonic"]) == T
    assert int(second["monotonic"]) == T * 2
