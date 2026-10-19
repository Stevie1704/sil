"""The perf and Heaptrack feasibility proof (issue #314).

`proofs/profiling/` captures real backend evidence on Linux x86-64 in CI.
These tests pin the disposable analysis that reads that evidence: perf
script parsing, the join of samples to host intervals, the Heaptrack file
decoding and the probe classification. Every expected value comes from a
hand-written fixture, not from the code under test.
"""

from __future__ import annotations

import importlib.util
import os
import struct
import subprocess
import sys
from collections import Counter

import pytest
from conftest import ROOT

from sil.recording import read_records

PROOF_DIR = ROOT / "proofs" / "profiling"


def _load(name: str, path):
    """Other proofs have a workload module too, so these get their own
    names. Dataclasses need the module in sys.modules."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


analyze = _load("profiling_analyze", PROOF_DIR / "analyze.py")
workload = _load("profiling_workload", PROOF_DIR / "workload.py")


@pytest.fixture(scope="module")
def workload_run(tmp_path_factory, sil_run):
    """One ordinary Run of the proof workload on this host, no profiler."""
    out = tmp_path_factory.mktemp("profiling-workload")
    artifacts = workload.build(out / "build")
    manifest = workload.manifest(artifacts).write(out / "manifest.json")
    sidecar = out / "sidecar"
    sidecar.mkdir()
    env = {**os.environ, "SILPROOF_SIDECAR_DIR": str(sidecar)}
    subprocess.run([str(sil_run), str(manifest.path), "-o",
                    str(out / "run.mcap")], check=True, env=env,
                   capture_output=True, text=True)
    return out


class TestWorkload:
    def test_each_participant_publishes_once_per_activation(
            self, workload_run):
        topics = Counter(topic for topic, _, _ in
                         read_records(workload_run / "run.mcap"))
        assert topics == {"proof.native": 100, "fmu.In": 100,
                          "proof.process": 100, "fmu.Out": 100}

    def test_the_shimmed_readings_are_virtual_time(self, workload_run):
        rows = [(t, struct.unpack("<4Q", data)) for topic, t, data in
                read_records(workload_run / "run.mcap",
                             channels=["proof.process"])]
        assert all(clock == t for t, (_, clock, _, _) in rows)
        children = [(t, child) for t, (_, _, child, result) in rows
                    if result]
        assert children == [(t, t) for t in (0, 250, 500, 750)
                            for t in [t * 1_000_000]]

    def test_every_hot_call_writes_one_interval(self, workload_run):
        intervals = [i for f in sorted((workload_run / "sidecar").iterdir())
                     for i in analyze.parse_sidecar(f.read_text())]
        assert Counter(i.function for i in intervals) == {
            "silproof_native_spin_even": 50, "silproof_native_spin_odd": 50,
            "silproof_process_spin": 100, "silproof_fmu_spin": 100,
            "silproof_child_spin": 4}
        assert all(i.begin_ns < i.end_ns for i in intervals)


PERF_SCRIPT = """\
sil-run  4100/4100 12.000001000:
\t    55d0a0001234 silproof_native_spin_even+0x24 (/w/lib/libsilproof_native.so)
\t    55d0a0001400 native_task+0x40 (/w/lib/libsilproof_native.so)
\t    55d0b0002000 sil::Engine::run+0x99 (/opt/sil/bin/sil-run)

python3  4200/4201 12.500000000:
\t    7f0000001000 [unknown] (/w/lib/libsilproof_process.so)

silproof_child 4300/4300 13.000000007:
\t    560000000100 silproof_child_spin (/w/bin/silproof_child)
"""


class TestPerfScript:
    def test_each_block_is_one_sample_with_exact_nanoseconds(self):
        samples = analyze.parse_perf_script(PERF_SCRIPT)
        assert [(s.comm, s.pid, s.tid, s.time_ns) for s in samples] == [
            ("sil-run", 4100, 4100, 12_000_001_000),
            ("python3", 4200, 4201, 12_500_000_000),
            ("silproof_child", 4300, 4300, 13_000_000_007),
        ]

    def test_frames_keep_symbol_without_offset_and_module(self):
        first = analyze.parse_perf_script(PERF_SCRIPT)[0]
        assert [(f.symbol, f.dso) for f in first.frames] == [
            ("silproof_native_spin_even", "/w/lib/libsilproof_native.so"),
            ("native_task", "/w/lib/libsilproof_native.so"),
            ("sil::Engine::run", "/opt/sil/bin/sil-run"),
        ]
        assert first.leaf.symbol == "silproof_native_spin_even"

    def test_a_sample_without_call_chain_has_its_frame_on_the_header(self):
        text = ("sil-run 7/8 1.000000002:  55d0a0001234 "
                "silproof_native_spin_odd+0x1 (/w/lib/n.so)\n")
        (sample,) = analyze.parse_perf_script(text)
        assert sample.time_ns == 1_000_000_002
        assert sample.frames == (
            analyze.Frame(0x55D0A0001234, "silproof_native_spin_odd",
                          "/w/lib/n.so"),)

    def test_an_unknown_frame_has_no_symbol(self):
        second = analyze.parse_perf_script(PERF_SCRIPT)[1]
        assert second.leaf.symbol is None
        assert second.leaf.dso == "/w/lib/libsilproof_process.so"


SIDECAR = (
    "native\t0\tsilproof_native_spin_even\t100\t200\t4100\t4100\n"
    "native\t1\tsilproof_native_spin_odd\t300\t400\t4100\t4100\n"
)


def _sample(symbol, time_ns, pid=4100):
    frame = analyze.Frame(0, symbol, "/w/lib/n.so")
    return analyze.Sample("sil-run", pid, pid, time_ns, (frame,))


class TestIntervalJoin:
    def test_sidecar_lines_become_intervals(self):
        assert analyze.parse_sidecar(SIDECAR) == [
            analyze.Interval("native", 0, "silproof_native_spin_even",
                             100, 200, 4100, 4100),
            analyze.Interval("native", 1, "silproof_native_spin_odd",
                             300, 400, 4100, 4100),
        ]

    def test_intervals_are_half_open(self):
        intervals = analyze.parse_sidecar(SIDECAR)
        samples = [_sample("silproof_native_spin_even", 100),
                   _sample("silproof_native_spin_even", 199),
                   _sample("silproof_native_spin_even", 200)]
        join = analyze.join_samples(samples, intervals)
        assert join["silproof_native_spin_even"] == analyze.JoinCount(
            inside=2, outside=1, inside_other_function=0)

    def test_a_sample_in_another_functions_interval_is_counted(self):
        intervals = analyze.parse_sidecar(SIDECAR)
        join = analyze.join_samples(
            [_sample("silproof_native_spin_even", 350)], intervals)
        assert join["silproof_native_spin_even"] == analyze.JoinCount(
            inside=0, outside=1, inside_other_function=1)

    def test_a_sample_of_another_process_is_not_joined_by_time(self):
        intervals = analyze.parse_sidecar(SIDECAR)
        join = analyze.join_samples(
            [_sample("silproof_native_spin_even", 150, pid=9)], intervals)
        assert join["silproof_native_spin_even"] == analyze.JoinCount(
            inside=0, outside=1, inside_other_function=0)

    def test_functions_without_intervals_are_not_reported(self):
        join = analyze.join_samples([_sample("memcpy", 150)],
                                    analyze.parse_sidecar(SIDECAR))
        assert "memcpy" not in join


# A Heaptrack 1.4 data file as heaptrack_interpret writes it (file format
# 3): hexadecimal numbers, sized strings, 1-based string, IP and trace
# indices and 0-based allocation-info indices. Trace 2 is the allocation
# site called from the task; trace 1 is the task alone.
HEAPTRACK = """\
v 10400 3
X /w/bin/sil-run
I 1000 3e8000
s b /w/lib/n.so
s 1a silproof_native_alloc_site
s 11 silproof_native.c
s b native_task
i 7f0000002000 1 4
i 7f0000001000 1 2 3 2a
t 1 0
t 2 1
a 400 2
+ 0
+ 0
- 0
c a
a 10000 1
+ 1
R 40
c 14
"""


class TestHeaptrackFile:
    def test_counts_and_bytes_through_a_function_include_its_callees(self):
        capture = analyze.parse_heaptrack(HEAPTRACK)
        assert capture.allocations_through("silproof_native_alloc_site") == (
            analyze.SiteCount(calls=2, bytes=0x800, outstanding_calls=1,
                              outstanding_bytes=0x400))
        assert capture.allocations_through("native_task") == (
            analyze.SiteCount(calls=3, bytes=0x10800, outstanding_calls=2,
                              outstanding_bytes=0x10400))

    def test_a_function_without_allocations_counts_zero(self):
        capture = analyze.parse_heaptrack(HEAPTRACK)
        assert capture.allocations_through("main") == analyze.SiteCount()

    def test_header_identity_and_timestamps_in_milliseconds(self):
        capture = analyze.parse_heaptrack(HEAPTRACK)
        assert capture.file_version == 3
        assert capture.exe == "/w/bin/sil-run"
        assert capture.timestamps_ms == [10, 20]
        assert capture.sizes_through("silproof_native_alloc_site") == {0x400}

    def test_an_inlined_frame_counts_as_its_own_function(self):
        text = HEAPTRACK.replace(
            "i 7f0000001000 1 2 3 2a",
            "i 7f0000001000 1 4 3 10 2 3 2a")
        capture = analyze.parse_heaptrack(text)
        assert capture.allocations_through(
            "silproof_native_alloc_site").calls == 2


class TestProbeOutcome:
    def test_a_zero_exit_with_data_is_available(self):
        assert analyze.probe_outcome(0, "", produced=True) == "available"

    def test_a_zero_exit_without_data_failed(self):
        assert analyze.probe_outcome(0, "", produced=False) == "failed"

    def test_paranoid_and_capability_refusals_are_permission_denied(self):
        for stderr in (
            "No permission to enable cpu-clock:u event.\n"
            "Consider tweaking /proc/sys/kernel/perf_event_paranoid",
            "Access to performance monitoring and observability operations "
            "is limited.",
            "perf_event_open(..., PERF_FLAG_FD_CLOEXEC) failed with "
            "unexpected error 1 (Operation not permitted)",
        ):
            assert analyze.probe_outcome(255, stderr, produced=False) == (
                "permission-denied"), stderr

    def test_an_unsupported_event_is_unsupported(self):
        assert analyze.probe_outcome(
            129, "The cpu-clock:u event is not supported.",
            produced=False) == "unsupported"

    def test_no_tool_is_missing(self):
        assert analyze.probe_outcome(None, "", produced=False) == "missing"
        assert analyze.probe_outcome(
            2, "WARNING: perf not found for kernel 6.8.0-1017-azure",
            produced=False) == "missing"

    def test_anything_else_failed(self):
        assert analyze.probe_outcome(1, "segfault", produced=False) == (
            "failed")


class TestBuildIds:
    def test_readelf_notes_give_the_gnu_build_id(self):
        notes = ("Displaying notes found in: .note.gnu.build-id\n"
                 "  Owner  Data size  Description\n"
                 "  GNU    0x00000014 NT_GNU_BUILD_ID (unique build ID)\n"
                 "    Build ID: 7c1f0e2a9d\n")
        assert analyze.readelf_build_id(notes) == "7c1f0e2a9d"
        assert analyze.readelf_build_id("no notes\n") is None

    def test_perf_buildid_list_maps_each_module_to_its_id(self):
        listing = ("7c1f0e2a9d /w/lib/libsilproof_native.so\n"
                   "0011aa [kernel.kallsyms]\n"
                   "ffee00 /r/a b/fmu/binaries/x86_64-linux/E.so\n")
        assert analyze.parse_buildid_list(listing) == {
            "/w/lib/libsilproof_native.so": "7c1f0e2a9d",
            "[kernel.kallsyms]": "0011aa",
            "/r/a b/fmu/binaries/x86_64-linux/E.so": "ffee00",
        }
