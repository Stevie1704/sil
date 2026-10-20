"""Normalization of a perf CPU capture (#315), pinned on synthetic exports.

The input is the text of the two `perf script` passes that `sil profile`
runs (docs/profile-report.md, "What the command does"), in the format of
perf 6.1 and 6.17 (tools/perf/builtin-script.c, util/event.c). The expected
values are counted by hand from that text: they do not come from the code.
"""

from __future__ import annotations

import json

import pytest

from sil.profile_cpu import ModuleFacts, normalize_cpu
from sil.profile_report import validate_profile_report

from conftest import ROOT

NATIVE = "/work/lib/libnative.so"
RUNNER = "/opt/sil/bin/sil-run-diagnostics"
CHILD = "/work/bin/child"
FMU = "/work/.sil-run-abc123/participant-1/binaries/x86_64-linux/fmu.so"
STRIPPED = "/work/lib/libstripped.so"

# Origin 9007199254.740993000 s: above 2^53 ns, so it must stay a string.
T0 = 9007199254


def t(offset_us: int) -> str:
    seconds, micros = divmod(offset_us, 1_000_000)
    return f"{T0 + seconds}.{740993000 + micros * 1000:09d}"


def header(comm: str, pid: int, tid: int, offset_us: int) -> str:
    return f"{comm:>16} {pid:>7}/{tid:<7} {t(offset_us)}:"


def mmap(comm, pid, offset_us, start, size, pgoff, build_id, path, prot="r-xp"):
    return (f"{header(comm, pid, pid, offset_us)} PERF_RECORD_MMAP2 "
            f"{pid}/{pid}: [{start}({size}) @ {pgoff} <{build_id}>]: "
            f"{prot} {path}")


def sample(comm, pid, tid, offset_us, period, frames):
    lines = [f"{header(comm, pid, tid, offset_us)} {period:>10} "]
    lines += [f"\t{ip:>16x} {sym} ({dso})" for ip, sym, dso in frames]
    return "\n".join(lines) + "\n"


ENGINE = (0x555500001100, "sil::Engine::run()", RUNNER)
MAIN = (0x555500001000, "main", RUNNER)
TASK = (0x7f0000001200, "native_task", NATIVE)

SCRIPT = "\n".join([
    # The launcher, perf's workload, execs; the runner starts as its child.
    f"{header('perf', 100, 100, 0)} PERF_RECORD_COMM exec: sil-diagnostics:100/100",
    f"{header('sil-diagnostics', 101, 101, 10)} PERF_RECORD_FORK(101:101):(100:100)",
    f"{header('sil-diagnostics', 101, 101, 20)} PERF_RECORD_COMM exec: sil-run-diagnos:101/101",
    mmap("sil-run-diagnos", 101, 30, "0x555500000000", "0x10000", "0x1000",
         "aa" * 20, RUNNER),
    mmap("sil-run-diagnos", 101, 40, "0x7f0000000000", "0x4000", "0x1000",
         "bb" * 20, NATIVE),
    mmap("sil-run-diagnos", 101, 41, "0x7f0000100000", "0x4000", "0",
         "bb" * 20, NATIVE, prot="r--p"),
    # A worker thread of the runner.
    f"{header('sil-run-diagnos', 101, 102, 50)} PERF_RECORD_FORK(101:102):(101:101)",
    sample("sil-run-diagnos", 101, 101, 100, 1000000, [
        (0x7f0000001400, "spin", NATIVE), TASK, ENGINE, MAIN]),
    # A recursive frame: the inclusive weight of `spin` counts it once.
    sample("sil-run-diagnos", 101, 101, 1100, 1000000, [
        (0x7f0000001404, "spin", NATIVE), (0x7f0000001408, "spin", NATIVE),
        TASK, ENGINE, MAIN]),
    sample("sil-run-diagnos", 101, 102, 2100, 2000000, [
        (0x555500001300, "sil::sha256_file(std::filesystem::path const&)",
         RUNNER), MAIN]),
    # A Process participant: it forks, execs and loads a module from the Run
    # working directory.
    f"{header('sil-run-diagnos', 103, 103, 3000)} PERF_RECORD_FORK(103:103):(101:101)",
    f"{header('sil-run-diagnos', 103, 103, 3010)} PERF_RECORD_COMM exec: python3:103/103",
    mmap("python3", 103, 3020, "0x7f1000000000", "0x2000", "0x1000",
         "cc" * 20, FMU),
    mmap("python3", 103, 3021, "0x7f2000000000", "0x2000", "0x1000",
         "dd" * 20, STRIPPED),
    sample("python3", 103, 103, 4000, 1000000, [
        (0x7f1000000100, "fmu_spin", FMU), (0x7f1000000200, "fmi3DoStep", FMU)]),
    sample("python3", 103, 103, 5000, 1000000, [
        (0x7f2000000100, "[unknown]", STRIPPED)]),
    sample("python3", 103, 103, 6000, 1000000, [
        (0xffffffff81000000, "[unknown]", "[unknown]")]),
    # A short-lived child of the Process participant.
    f"{header('python3', 104, 104, 7000)} PERF_RECORD_FORK(104:104):(103:103)",
    f"{header('python3', 104, 104, 7010)} PERF_RECORD_COMM exec: child:104/104",
    mmap("child", 104, 7020, "0x5600000000", "0x1000", "0x1000", "ee" * 20,
         CHILD),
    sample("child", 104, 104, 7500, 1000000, [
        (0x5600000100, "child_spin", CHILD)]),
    f"{header('child', 104, 104, 8000)} PERF_RECORD_EXIT(104:104):(103:103)",
    # The kernel reuses PID 104 for a second child: a new process identity.
    f"{header('python3', 104, 104, 9000)} PERF_RECORD_FORK(104:104):(103:103)",
    f"{header('python3', 104, 104, 9100)} PERF_RECORD_EXIT(104:104):(103:103)",
    f"{header('python3', 103, 103, 9500)} PERF_RECORD_LOST lost 7",
    f"{header('python3', 103, 103, 9600)} PERF_RECORD_EXIT(103:103):(101:101)",
]) + "\n"

# `perf script -G -F ip,sym,srcline`: the leaf of each sample, in order.
SRCLINES = """\
    7f0000001400 spin
  native.c:40
    7f0000001404 spin
  native.c:41
    555500001300 sil::sha256_file(std::filesystem::path const&)
  sil-run-diagnostics[1300]
    7f1000000100 fmu_spin
  fmu.c:12
    7f2000000100 [unknown]
  stripped.c:9
ffffffff81000000 [unknown]
  [unknown]
    5600000100 child_spin
  child.c:5
"""

FACTS = {
    RUNNER: ModuleFacts("host_file", "not_found", None),
    NATIVE: ModuleFacts("host_file", "separate_matching", NATIVE + ".debug"),
    FMU: ModuleFacts("captured_modules_from", "embedded", None),
    # A mismatched debug file: perf printed a line, the contract drops it.
    STRIPPED: ModuleFacts("host_file", "separate_mismatched", None),
    CHILD: ModuleFacts("host_file", "embedded", None),
}


def facts(path: str, build_id: str | None) -> ModuleFacts:
    return FACTS[path]


@pytest.fixture(scope="module")
def analysis():
    return normalize_cpu(SCRIPT, srclines=SRCLINES, facts=facts,
                         lost_sample_records=0)


def test_totals_count_every_sample_and_its_period_weight(analysis):
    assert analysis.totals == {
        "samples": 7, "weight_ns": 8_000_000, "samples_unknown_module": 1,
        "samples_unknown_symbol": 1, "samples_with_caller": 4,
        "lost_samples": 7, "lost_records": 1, "unavailable": {}}


def test_time_origin_is_the_first_record_as_an_exact_string(analysis):
    assert analysis.time_origin_ns == "9007199254740993000"


def test_processes_keep_lifetimes_roles_and_reuse_of_a_pid(analysis):
    rows = [(p["id"], p["pid"], p["parent"], p["role"], p["commands"],
             p["execs"], p["forked"], p["exited"], p["samples"],
             p["weight_ns"], p["threads"]) for p in analysis.processes]

    assert rows == [
        ("p1", 100, None, "launcher", ["perf", "sil-diagnostics"], 1,
         False, False, 0, 0, 1),
        ("p2", 101, "p1", "runner", ["sil-diagnostics", "sil-run-diagnos"],
         1, True, False, 3, 4_000_000, 2),
        ("p3", 103, "p2", "descendant", ["sil-run-diagnos", "python3"], 1,
         True, True, 3, 3_000_000, 1),
        ("p4", 104, "p3", "descendant", ["python3", "child"], 1, True, True,
         1, 1_000_000, 1),
        # Observed by its fork and exit, with no sample: still listed.
        ("p5", 104, "p3", "descendant", ["python3"], 0, True, True, 0, 0, 1),
    ]
    assert analysis.processes[3]["start_offset_ns"] == 7_000_000
    assert analysis.processes[3]["end_offset_ns"] == 8_000_000


def test_threads_are_lifetimes_of_their_process(analysis):
    rows = [(t["id"], t["process"], t["tid"], t["samples"])
            for t in analysis.threads]

    assert rows == [("t1", "p1", 100, 0), ("t2", "p2", 101, 2),
                    ("t3", "p2", 102, 1), ("t4", "p3", 103, 3),
                    ("t5", "p4", 104, 1), ("t6", "p5", 104, 0)]


def test_modules_keep_build_ids_executable_mappings_and_facts(analysis):
    modules = {m["path"]: m for m in analysis.modules}

    assert [m["path"] for m in analysis.modules] == sorted(modules)
    native = modules[NATIVE]
    assert native["build_id"] == "bb" * 20
    assert native["mappings"] == [{"process": "p2", "start": "0x7f0000000000",
                                   "size": "0x4000", "file_offset": "0x1000"}]
    assert native["debug"] == {"status": "separate_matching",
                               "path": NATIVE + ".debug"}
    assert (native["samples_leaf"], native["samples_unknown_symbol"]) == (2, 0)
    assert modules[FMU]["material"] == "captured_modules_from"
    assert (modules[STRIPPED]["samples_leaf"],
            modules[STRIPPED]["samples_unknown_symbol"]) == (1, 1)


def test_self_hotspots_rank_leaf_weight_and_keep_a_remainder():
    limited = normalize_cpu(SCRIPT, srclines=SRCLINES, facts=facts,
                            lost_sample_records=0, hotspot_limit=2)
    by_id = {m["id"]: m["path"] for m in limited.modules}
    rows = [(by_id.get(r["module"]), r["symbol"], r["self_samples"],
             r["self_weight_ns"]) for r in limited.hotspots["self"]]

    # Equal weights rank by module path, then symbol.
    assert rows == [
        (RUNNER, "sil::sha256_file(std::filesystem::path const&)", 1,
         2_000_000),
        (NATIVE, "spin", 2, 2_000_000)]
    assert limited.hotspots["self_remainder"] == {"samples": 4,
                                                  "weight_ns": 4_000_000}


def test_inclusive_weight_counts_a_function_once_per_sample(analysis):
    by_id = {m["id"]: m["path"] for m in analysis.modules}
    inclusive = {(by_id.get(r["module"]), r["symbol"]):
                 (r["inclusive_samples"], r["inclusive_weight_ns"])
                 for r in analysis.hotspots["inclusive"]}

    assert inclusive[(NATIVE, "spin")] == (2, 2_000_000)
    assert inclusive[(RUNNER, "main")] == (3, 4_000_000)
    assert inclusive[(FMU, "fmi3DoStep")] == (1, 1_000_000)


def test_source_lines_only_from_matching_or_embedded_debug(analysis):
    by_id = {m["id"]: m["path"] for m in analysis.modules}
    lines = {(by_id.get(r["module"]), r["symbol"]): r["source_lines"]
             for r in analysis.hotspots["self"]}

    assert lines[(NATIVE, "spin")] == [
        {"file": "native.c", "line": 40, "samples": 1},
        {"file": "native.c", "line": 41, "samples": 1}]
    assert lines[(FMU, "fmu_spin")] == [
        {"file": "fmu.c", "line": 12, "samples": 1}]
    # perf printed stripped.c:9, but the debug file has another build ID.
    assert lines[(STRIPPED, None)] == []
    assert analysis.symbolization["source_lines"] == {
        "status": "partial", "scope": "leaf_frames", "samples_with_line": 4,
        "unavailable": {}}


def test_symbolization_reports_names_and_unwinding_separately(analysis):
    assert analysis.symbolization["function_names"] == {
        "samples_with_name": 5}
    assert analysis.symbolization["call_frame_unwinding"] == {
        "method": "dwarf", "samples_with_caller": 4}
    assert analysis.symbolization["inlined_frames"] == {
        "status": "available", "samples_with_inlined_frames": 0}


def test_samples_document_keeps_chronology_and_module_offsets(analysis):
    document = analysis.samples_document
    rows = document["samples"]

    assert document["sil_profile_cpu_samples"] == 1
    assert document["time_origin_ns"] == "9007199254740993000"
    assert [row[0] for row in rows] == [100_000, 1_100_000, 2_100_000,
                                        4_000_000, 5_000_000, 6_000_000,
                                        7_500_000]
    assert [(row[1], row[2], row[3]) for row in rows[:3]] == [
        ("p2", "t2", 1_000_000), ("p2", "t2", 1_000_000),
        ("p2", "t3", 2_000_000)]
    leaf = document["frames"][document["stacks"][rows[0][4]][0]]
    assert leaf == {"module": analysis.module_id(NATIVE), "symbol": "spin",
                    "address": "0x7f0000001400", "module_offset": "0x2400",
                    "source": {"file": "native.c", "line": 40}}
    unknown = document["frames"][document["stacks"][rows[5][4]][0]]
    assert unknown == {"module": None, "symbol": None,
                       "address": "0xffffffff81000000",
                       "module_offset": None, "source": None}


def test_without_a_srcline_pass_source_lines_are_unavailable():
    result = normalize_cpu(SCRIPT, srclines=None, facts=facts,
                           lost_sample_records=0,
                           srcline_problem="symbolization_timeout")

    assert result.symbolization["source_lines"] == {
        "status": "unavailable", "scope": "leaf_frames",
        "samples_with_line": None,
        "unavailable": {"samples_with_line": "symbolization_timeout"}}
    assert all(r["source_lines"] == [] for r in result.hotspots["self"])


def test_a_srcline_pass_that_does_not_pair_is_not_used():
    result = normalize_cpu(SCRIPT, srclines=SRCLINES.split("    5600")[0],
                           facts=facts, lost_sample_records=0)

    assert result.symbolization["source_lines"]["status"] == "unavailable"
    assert result.symbolization["source_lines"]["unavailable"] == {
        "samples_with_line": "export_failed"}


def test_lost_sample_records_without_a_count_make_the_count_unknown():
    result = normalize_cpu(SCRIPT, srclines=SRCLINES, facts=facts,
                           lost_sample_records=1)

    assert result.totals["lost_samples"] is None
    assert result.totals["lost_records"] == 2
    assert result.totals["unavailable"] == {
        "lost_samples": "not_reported_by_backend"}


def test_zero_samples_give_empty_tables_not_zero_cpu_claims():
    script = "\n".join(SCRIPT.splitlines()[:3]) + "\n"

    result = normalize_cpu(script, srclines="", facts=facts,
                           lost_sample_records=0)

    assert result.totals["samples"] == 0
    assert result.hotspots["self"] == []
    assert result.symbolization["source_lines"]["unavailable"] == {
        "samples_with_line": "no_samples"}
    assert [p["role"] for p in result.processes] == ["launcher", "runner"]


def test_an_empty_export_has_no_time_origin():
    result = normalize_cpu("", srclines="", facts=facts,
                           lost_sample_records=0)

    assert result.time_origin_ns is None
    assert result.processes == []


def test_a_normalized_capture_fits_the_report_contract(analysis):
    report = json.loads(
        (ROOT / "tests" / "fixtures" / "profile" / "cpu-complete.json")
        .read_text())
    capture = report["capture"]
    capture.update(time_origin_ns=analysis.time_origin_ns,
                   totals=analysis.totals,
                   symbolization=analysis.symbolization,
                   processes=analysis.processes, threads=analysis.threads,
                   modules=analysis.modules, hotspots=analysis.hotspots)
    capture.update(status="partial", reasons=["lost_samples"])

    assert validate_profile_report(report) == []


# perf prints a sample with no frame when DWARF unwinding cannot start: the
# module of its address is not available (perf 6.17, a removed FMU library).
FRAMELESS = "\n".join([
    mmap("python3", 300, 0, "0x7f1000000000", "0x2000", "0x1000", "cc" * 20,
         FMU),
    f"{header('python3', 300, 300, 100)} {1000000:>10} ",
    "",
]) + "\n"


def test_a_frameless_sample_takes_its_leaf_from_the_srcline_pass():
    result = normalize_cpu(FRAMELESS, srclines="    7f1000000180 [unknown]\n"
                           "  ??:0\n", facts=facts, lost_sample_records=0)

    fmu, = result.modules
    assert (fmu["path"], fmu["samples_leaf"],
            fmu["samples_unknown_symbol"]) == (FMU, 1, 1)
    leaf = result.samples_document["frames"][0]
    assert (leaf["address"], leaf["module_offset"]) == ("0x7f1000000180",
                                                        "0x1180")
    assert result.totals["samples_unknown_module"] == 0


def test_a_frameless_sample_without_a_leaf_stays_in_the_totals():
    result = normalize_cpu(FRAMELESS, srclines=None, facts=facts,
                           lost_sample_records=0,
                           srcline_problem="symbolization_timeout")

    assert result.totals["samples"] == 1
    assert result.totals["samples_unknown_module"] == 1
    assert result.hotspots["self"] == [{
        "module": None, "symbol": None, "self_samples": 1,
        "self_weight_ns": 1000000, "inclusive_samples": 1,
        "inclusive_weight_ns": 1000000, "source_lines": []}]
    assert result.samples_document["stacks"] == [[]]


# perf script --inline: an inlined function is a line with the physical
# frame's address, marked "(inlined)" and without a module, innermost first,
# before its physical frame.
INLINED = "\n".join([
    mmap("sil-run-diagnos", 101, 0, "0x7f0000000000", "0x4000", "0x1000",
         "bb" * 20, NATIVE),
    f"{header('sil-run-diagnos', 101, 101, 100)} {1000000:>10} ",
    "\t    7f0000001400 mix (inlined)",
    "\t    7f0000001400 square (inlined)",
    f"\t    7f0000001400 spin ({NATIVE})",
    f"\t    7f0000001200 native_task ({NATIVE})",
    "",
    # One physical frame: an inlined function is no caller.
    f"{header('sil-run-diagnos', 101, 101, 200)} {1000000:>10} ",
    "\t    7f0000001400 square (inlined)",
    f"\t    7f0000001400 spin ({NATIVE})",
    "",
]) + "\n"


def test_inlined_frames_are_own_functions_counted_once_per_sample():
    srclines = ("    7f0000001400 spin\n  native.c:7\n"
                "    7f0000001400 spin\n  native.c:8\n")
    result = normalize_cpu(INLINED, srclines=srclines, facts=facts,
                           lost_sample_records=0)

    native = result.module_id(NATIVE)
    rows = {r["symbol"]: r for r in result.hotspots["inclusive"]}
    assert {s: (r["self_samples"], r["inclusive_samples"])
            for s, r in rows.items()} == {
        "mix": (1, 1), "square": (1, 2), "spin": (0, 2),
        "native_task": (0, 1)}
    assert all(r["module"] == native for r in rows.values())
    assert result.totals["samples_with_caller"] == 1
    assert result.symbolization["inlined_frames"] == {
        "status": "available", "samples_with_inlined_frames": 2}
    document = result.samples_document
    leaf = document["frames"][document["stacks"][0][0]]
    assert leaf == {"module": native, "symbol": "mix", "inlined": True,
                    "address": "0x7f0000001400", "module_offset": "0x2400",
                    "source": {"file": "native.c", "line": 7}}
    physical = document["frames"][document["stacks"][0][2]]
    assert physical["symbol"] == "spin" and "inlined" not in physical
    assert [len(stack) for stack in document["stacks"]] == [4, 2]


# perf 6.17 marks every function of an address "(inlined)" when the
# function name of the debug data differs from the symbol (for example a
# .constprop clone): then no physical frame line follows.
ALL_INLINED = "\n".join([
    mmap("sil-run-diagnos", 101, 0, "0x7f0000000000", "0x4000", "0x1000",
         "bb" * 20, NATIVE),
    f"{header('sil-run-diagnos', 101, 101, 100)} {1000000:>10} ",
    "\t    7f0000001400 square (inlined)",
    "\t    7f0000001400 spin (inlined)",
    f"\t    7f0000001200 native_task ({NATIVE})",
    "",
]) + "\n"


def test_an_address_of_inlined_frames_only_takes_its_mapped_module():
    result = normalize_cpu(ALL_INLINED, srclines=None, facts=facts,
                           lost_sample_records=0,
                           srcline_problem="symbolization_timeout")

    native = result.module_id(NATIVE)
    document = result.samples_document
    stack = [document["frames"][i] for i in document["stacks"][0]]
    assert [(f["symbol"], f["module"], f.get("inlined", False))
            for f in stack] == [("square", native, True),
                                ("spin", native, True),
                                ("native_task", native, False)]
    # Two addresses: native_task is the one caller.
    assert result.totals["samples_with_caller"] == 1
    assert result.totals["samples_unknown_module"] == 0


def test_a_failed_inline_export_names_its_reason():
    result = normalize_cpu(SCRIPT, srclines=SRCLINES, facts=facts,
                           lost_sample_records=0,
                           inlined_problem="inlined_frames_failed")

    assert result.symbolization["inlined_frames"] == {
        "status": "unavailable", "samples_with_inlined_frames": None,
        "unavailable": {"samples_with_inlined_frames":
                        "inlined_frames_failed"}}


def test_the_export_can_be_normalized_line_by_line(analysis):
    # sil profile streams the perf script pass: it never holds its text.
    lines = iter(SCRIPT.splitlines(keepends=True))

    streamed = normalize_cpu(lines, srclines=SRCLINES, facts=facts,
                             lost_sample_records=0)

    assert streamed == analysis
