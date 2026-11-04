#!/usr/bin/env python3
"""Issue #361 experiment. Proof controls only; no production CLI or schema.

One fresh Run/collector per capture. All subprocesses and buffers are bounded.
The runner sends nonblocking notices; only this supervisor waits for perf ack.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time

from sil.manifest import Manifest
from sil.profile_storage import RESERVE_BYTES
from model import CASES, EVENT, MAX_CAPTURE_BYTES, PERIOD_NS, RUN_DURATION_NS, perf_sample_host_ns
from session import CaptureSession, GUARD_POLL_NS, SUPERVISOR_TIMEOUT_NS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SAMPLE_OPTIONS = ["-e", EVENT, "-F", "997", "-k", "monotonic",
                  "--call-graph", "dwarf,8192", "--buildid-mmap", "-N", "-q"]
EXPORT_BYTES = 256 << 20


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(argv, out, *, timeout=180):
    """Retain bounded tool output; abort a whole tool session on timeout."""
    with out.open("wb") as output:
        process = subprocess.Popen([str(a) for a in argv], stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        until = time.monotonic() + timeout
        while process.poll() is None:
            if time.monotonic() > until or output.tell() > EXPORT_BYTES:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise RuntimeError(f"tool exceeded time/output budget: {argv}")
            time.sleep(.02)
        if output.tell() > EXPORT_BYTES:
            raise RuntimeError(f"tool exceeded output budget: {argv}")
    return process.returncode


def build(work):
    work.mkdir(parents=True, exist_ok=False)
    original = (ROOT / "kernel/src/run_diagnostics.cpp").read_text()
    patched = '#include "notify.hpp"\n' + original
    before = ': task_(task), slot_ns_(slot_ns), start_ns_(now_ns()) {}'
    after = (': task_(task), slot_ns_(slot_ns), start_ns_(now_ns()) {\n'
             '  window_proof::notify(task_, slot_ns_, 0);\n}')
    if patched.count(before) != 1:
        raise ValueError("diagnostics constructor anchor changed")
    patched = patched.replace(before, after)
    before = '  const uint64_t end = now_ns();\n'
    # completed() and the unfinished destructor; not any other measurement.
    for signature in ('void Activation::completed() noexcept {',
                      'Activation::~Activation() {\n  if (done_) return;'):
        anchor = signature + '\n' + before
        if patched.count(anchor) != 1:
            raise ValueError("diagnostics completion anchor changed")
        patched = patched.replace(anchor, signature + '\n'
            '  window_proof::notify(task_, slot_ns_, 1);\n' + before)
    source = work / "run_diagnostics.cpp"
    source.write_text(patched)
    commands = [
        ["cmake", "-S", ROOT, "-B", work / "build", "-DCMAKE_BUILD_TYPE=Release",
         ],
        ["cmake", "--build", work / "build", "--target", "sil-run",
         "sil-run-diagnostics", "-j2"],
        # Keep a genuinely unmodified diagnostics baseline, including its
        # exact binary identity. Reconfigure reuses every other object file.
        ["cp", work / "build/sil-run-diagnostics", work / "diagnostics-baseline"],
        ["cmake", "-S", ROOT, "-B", work / "build", "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_PROJECT_INCLUDE={HERE / 'overlay.cmake'}",
         f"-DWINDOW_PROOF_SOURCE={source}"],
        ["cmake", "--build", work / "build", "--target", "sil-run",
         "sil-run-diagnostics", "-j2"],
        ["cc", "-O2", "-g", "-fno-omit-frame-pointer", "-fPIC", "-shared",
         "-I", ROOT / "include", HERE / "workload.c", "-o", work / "workload.so"],
    ]
    for index, argv in enumerate(commands):
        if command(argv, work / f"build-{index}.txt", timeout=600):
            raise RuntimeError(f"build failed: {argv}")
    write(work / "commands.json", [[str(a) for a in c] for c in commands])


def manifest(work, variant, fail=False):
    m = Manifest(duration_ns=RUN_DURATION_NS)
    m.add_schemas({"Event": {"fields": [{"name": "task", "type": "u64"},
                                           {"name": "index", "type": "u64"}]}})
    m.add_channel("events", schema="Event")
    if variant == "native":
        m.add_native("work", library=str(work / "workload.so"),
                     publishes=["events"], config={"fail": fail})
    else:
        m.add_process("work", command=[sys.executable, str(HERE / "process.py"),
                      str(work / "workload.so"), "fail" if fail else "pass"],
                      step_period_ns=PERIOD_NS, publishes=["events"])
    path = work / f"{variant}-{'fail' if fail else 'pass'}.json"
    if not path.exists():
        m.write(path)
    return path


def perf_sample_host_times(path):
    """Integer host timestamps from perf's --ns sample pass."""
    return [host_ns for line in path.read_text().splitlines()
            if (host_ns := perf_sample_host_ns(line)) is not None]


def runner_command(out, work, variant, case):
    source = manifest(work, variant, case.failing_workload)
    runners = {"ordinary": work / "build/sil-run", "baseline": work / "diagnostics-baseline",
               "observed": work / "build/sil-run-diagnostics"}
    argv = [str(runners[case.runner]), str(source), "-o", str(out / "run.mcap"),
            "--participant-timeout-ms", "10000"]
    if case.runner != "ordinary":
        argv += ["--diagnostics-out", str(out / "diagnostics.json"),
                 "--activation-observations", str(out / "activations.jsonl"),
                 "--activation-observations-limit-bytes", str(8 << 20)]
    return argv


def metadata_counts(path, controls):
    metadata = path.read_text(errors="replace")
    return {"exec_records": metadata.count("PERF_RECORD_COMM exec"),
            "fork_records": metadata.count("PERF_RECORD_FORK"),
            "exit_records": metadata.count("PERF_RECORD_EXIT"),
            "workload_mapping": "workload.so" in metadata,
            "first_enable_write_ns": next((c["write_ns"] for c in controls if c["command"] == "enable"), None),
            "lost_records": metadata.count("PERF_RECORD_LOST"),
            "throttle_records": metadata.count("PERF_RECORD_THROTTLE")}


def export_perf(out, data, result, case):
    passes = {
        "samples.txt": ["script", "--ns", "-F", "time", "--hide-call-graph"],
        "symbols.txt": ["script", "--ns", "-F", "time,ip,sym,dso", "--hide-call-graph"],
        "metadata.txt": ["script", "--ns", "--show-mmap-events", "--show-task-events",
                         "--show-lost-events", "--hide-call-graph"],
        "stats.txt": ["report", "--stdio", "--stats"],
        "events.txt": ["evlist", "-v"], "buildids.txt": ["buildid-list", "--with-hits"]}
    result["exports"] = {name: command(["perf", *args, "-i", data], out / name)
                         for name, args in passes.items()}
    if case.outcome != "storage_fault" and any(result["exports"].values()):
        write(out / "result.json", result)
        raise RuntimeError(f"incomplete perf export: {out}")
    host_times = perf_sample_host_times(out / "samples.txt")
    write(out / "perf-sample-host-times.json", host_times)
    result["samples"] = {"count": len(host_times),
                         "first_host_ns": min(host_times) if host_times else None,
                         "last_host_ns": max(host_times) if host_times else None}
    result["metadata"] = metadata_counts(out / "metadata.txt", result["controls"])


def capture_result(out, case, variant, mode, data, session, code, elapsed_ns):
    diagnostics = out / "diagnostics.json"
    run_code = json.loads(diagnostics.read_text())["run"]["exit_code"] if diagnostics.exists() else None
    if case.runner == "ordinary":
        run_code = code
    result = {"variant": variant, "mode": mode, "requested": case.selection(variant).json() if case.gated else None,
              "exit_code": code, "run_exit_code": run_code, "elapsed_ns": elapsed_ns,
              "perf_bytes": data.stat().st_size if data.exists() else 0,
              "recording_sha256": digest(out / "run.mcap") if (out / "run.mcap").exists() else None,
              "policy": {"max_capture_bytes": (64 << 10) if case.fault == "capture_limit" else MAX_CAPTURE_BYTES,
                         "guard_poll_ns": GUARD_POLL_NS, "storage_reserve_bytes": RESERVE_BYTES,
                         "proof_supervisor_timeout_ns": SUPERVISOR_TIMEOUT_NS,
                         "participant_response_deadline_ms": 10000, "frequency_hz": 997,
                         "cpu_control": "named_event_preserves_tracking"},
              **session.observations()}
    write(out / "notices.json", session.notices)
    if (out / "observer.json").exists():
        result["observer"] = json.loads((out / "observer.json").read_text())
        result["observer"]["received"] = len(session.notices)
    return result


def capture(out, work, variant, mode, *, reserve_directory=None):
    out.mkdir()
    case = CASES[mode]
    data = reserve_directory / (out.name + ".data") if reserve_directory else out / "perf.data"
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "python/src"),
                   "SIL_WINDOW_MARKERS": str(out / "markers.json")}
    started = time.monotonic_ns()
    with CaptureSession(out, case, case.selection(variant), data, reserve_directory) as session:
        argv = session.collector_command(runner_command(out, work, variant, case), SAMPLE_OPTIONS)
        write(out / "command.json", argv)
        code = session.run(argv, environment)
        result = capture_result(out, case, variant, mode, data, session, code, time.monotonic_ns()-started)
    if case.collector and data.exists():
        export_perf(out, data, result, case)
        if reserve_directory:
            shutil.copy2(data, out / "perf.data")
            data.unlink()
            (reserve_directory / "filler").unlink(missing_ok=True)
    write(out / "result.json", result)
    return result


def write_identities(out, work, environment):
    identities = {"environment": environment, "uname": platform.uname()._asdict(),
                  "libc": platform.libc_ver(), "revision": os.environ.get("SIL_SOURCE_REVISION"),
                  "perf_event_paranoid": Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()}
    for label, args in {"perf": ["perf", "version"], "compiler": ["cc", "--version"],
                        "packages": ["dpkg-query", "-W"], "buildids": ["readelf", "-n", work / "workload.so"]}.items():
        command(args, out / f"{label}.txt")
    identities["binaries"] = {str(p): digest(p) for p in
        (work / "workload.so", work / "build/sil-run", work / "build/sil-run-diagnostics",
         work / "diagnostics-baseline")}
    identities["sources"] = {str(p.relative_to(ROOT)): digest(p) for p in
        (HERE / "workload.c", HERE / "notify.hpp", HERE / "qualify.py",
         HERE / "model.py", HERE / "session.py", HERE / "analyze.py", HERE / "validate.py",
         ROOT / "kernel/src/run_diagnostics.cpp")}
    identities["overlay_sha256"] = digest(work / "run_diagnostics.cpp")
    write(out / "identities.json", identities)


def check_functional_outcome(row, case, baselines):
    if case.runner == "ordinary":
        baselines[case.outcome] = row["recording_sha256"]
    if case.outcome in ("complete", "failed"):
        expected_code = 0 if case.outcome == "complete" else 1
        row["recording_equal"] = row["recording_sha256"] == baselines[case.outcome]
        if row["exit_code"] != expected_code or not row["recording_equal"]:
            raise RuntimeError(f"outcome/Recording changed: {row['variant']} {row['mode']}")
    else:
        row["recording_equal"] = row["recording_sha256"] == baselines["complete"]
    if case.required_reason == "storage_reserve" and row["stop_reason"] != "storage_reserve":
        raise RuntimeError("storage reserve not exercised")


def run_variant(out, work, variant, reserve_directory, results):
    baselines = {}
    for mode, case in CASES.items():
        if case.storage and reserve_directory is None:
            continue
        for repeat in range(case.repeats):
            destination = out / f"{variant}-{mode}-{repeat}"
            row = capture(destination, work, variant, mode,
                          reserve_directory=reserve_directory if case.storage else None)
            check_functional_outcome(row, case, baselines)
            results.append(row)
            print(f"{variant} {mode} {repeat}: exit={row['exit_code']} bytes={row['perf_bytes']}", flush=True)
            write(out / "results.json", {"decision": "pending_analysis", "runs": results})


def qualify(out, work, environment, reserve_directory):
    out.mkdir(parents=True, exist_ok=False)
    write_identities(out, work, environment)
    # Retain permission/feature availability before the experiment. No sysctl here.
    probe = command(["perf", "record", *SAMPLE_OPTIONS, "--delay=-1", "-o",
                     out / "probe.data", "--", "/bin/true"], out / "availability.txt")
    write(out / "availability.json", {"exit_code": probe, "available": probe == 0})
    if probe:
        write(out / "results.json", {"decision": "unavailable", "runs": []})
        return
    results = []
    for variant in ("native", "process"):
        run_variant(out, work, variant, reserve_directory, results)
    write(out / "digests.json", {str(p.relative_to(out)): digest(p)
                                for p in out.rglob("*") if p.is_file()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "run"))
    parser.add_argument("work", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--environment", default="unspecified")
    parser.add_argument("--small-dir", type=Path)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64" or platform.libc_ver()[0] != "glibc":
        parser.error("qualification requires Linux x86-64/glibc")
    if args.action == "build": build(args.work.resolve())
    else:
        if args.out is None: parser.error("run requires --out")
        qualify(args.out.resolve(), args.work.resolve(), args.environment, args.small_dir)
