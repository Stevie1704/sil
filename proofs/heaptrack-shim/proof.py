"""Disposable #330 experiment; no production profiling interface changes.

Run on Linux x86-64/glibc with a fresh output directory. Failed hypotheses
are observations. Only workload integrity and evidence completeness gate it.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

from evidence import command, digest, save

from sil.manifest import Manifest
from sil.profile_heap import normalize_heap
from sil.profile_heap_run import find_heaptrack
from sil.recording import read_records
from sil.schema import MessageType

HERE = Path(__file__).resolve().parent
SCHEMA = {"fields": [{"name": name, "type": kind} for name, kind in (
    ("parent", "u64"), ("child", "u64"), ("grandchild", "u64"),
    ("sleep_status", "i32"), ("sleep_errno", "i32"), ("result", "u64"))]}
STEPS = 10
STEP_PERIOD_NS = 100_000_000
ALLOCATIONS_PER_STEP = 8
ALLOCATION_BYTES = 2048
WALL_LIMIT_SECONDS = 30
EXPECTED_SLEEP = {"reject": (-1, errno.ENOSYS), "immediate": (0, 0)}
CAP_BYTES = 128 * 1024 * 1024


def execute(args, directory, env, raw, *, raw_limit_bytes=CAP_BYTES):
    """Bound captures while letting the runner clean up Participant groups."""
    began = time.monotonic_ns()
    stopped = None
    cleanup_completed = True
    with (directory / "stdout.txt").open("wb") as stdout, \
            (directory / "stderr.txt").open("wb") as stderr:
        process = subprocess.Popen(list(map(str, args)), cwd=directory,
            env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic_ns() - began > WALL_LIMIT_SECONDS * 1_000_000_000:
                    stopped = "wall_timeout"
                elif raw.exists() and raw.stat().st_size > raw_limit_bytes:
                    stopped = "raw_size_limit"
                if stopped:
                    break
                time.sleep(0.02)
        finally:
            if process.poll() is None:
                # Participants have their own process groups. Killing just
                # the runner leaves them writing after capture finalization.
                # SIGTERM lets the runner interrupt its protocol wait and
                # apply its existing group cleanup before it returns.
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    cleanup_completed = False
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return {"command": list(map(str, args)), "exit_code": process.returncode,
            "host_begin_ns": began, "host_end_ns": time.monotonic_ns(),
            "wall_ns": time.monotonic_ns() - began, "stopped": stopped,
            "cleanup_completed": cleanup_completed}


def read_messages(path):
    codec = MessageType("proof.Clock", SCHEMA)
    return [{"t_ns": t, **codec.unpack(data)}
            for _, t, data in read_records(path)]


def analyze(raw, interpreter, directory):
    interpreted = directory / "heaptrack.data"
    with raw.open("rb") as stdin, interpreted.open("wb") as stdout, \
            (directory / "interpret.stderr.txt").open("wb") as stderr:
        done = subprocess.run([str(interpreter)], stdin=stdin, stdout=stdout,
                              stderr=stderr, timeout=60)
    result = {"interpret_exit_code": done.returncode,
              "interpreted": digest(interpreted)}
    if done.returncode:
        return result
    data = interpreted.read_bytes()
    timestamps = [int(line.split()[1], 16) for line in data.splitlines()
                  if line.startswith(b"c ")]
    result["timestamps"] = {
        "count": len(timestamps), "unique": len(set(timestamps)),
        "min_ms": min(timestamps, default=None),
        "max_ms": max(timestamps, default=None),
        "clock": None, "clock_assessment": "not_measured_in_this_capture",
        "source_hypothesis": "Heaptrack steady_clock; see candidates.md and clock calibration",
        "host_mapping": None}
    analysis = normalize_heap(data, build_id=lambda _: (None, "proof_only"))
    document = analysis.allocations_document
    matches = []
    for stack, size, calls, _, _ in document["allocations"]:
        names = {document["frames"][f]["symbol"]
                 for f in document["stacks"][stack]}
        if "proof_alloc_site" in names:
            matches.append((size, calls))
    result["known_site"] = {"calls": sum(calls for _, calls in matches),
        "sizes": sorted({size for size, calls in matches if calls}),
        "expected_calls": STEPS * ALLOCATIONS_PER_STEP, "expected_size": ALLOCATION_BYTES}
    result["coverage"] = analysis.record["coverage"]
    result["allocation_totals"] = analysis.record["totals"]
    return result


def validate_evidence(report):
    """Validate collection, keeping negative experiments explicitly incomplete."""
    expected = {(policy, mode) for policy in ("reject", "immediate")
                for mode in ("baseline", "preload", "preserve")}
    assert len(report["runs"]) == len(expected), "incomplete experiment matrix"
    assert {(r["policy"], r["mode"]) for r in report["runs"]} == expected
    for policy in ("reject", "immediate"):
        assert len({r["manifest"]["sha256"] for r in report["runs"]
                    if r["policy"] == policy}) == 1, "Manifest changed"
    observations = []
    for row in report["runs"]:
        assert row["exit_code"] in (0, 1), row
        if row["mode"] == "baseline":
            assert row["stopped"] is None and row["exit_code"] == 0, row
            assert row["messages"] == STEPS, row
            assert all(row["clocks_match_virtual_time"].values()), row
            assert row["sleep_matches_policy"], row
            status = "completed"
        elif row["stopped"] is not None:
            assert row["stopped"] in {"raw_size_limit", "wall_timeout"}, row
            assert row.get("cleanup_completed") is True, row
            assert row.get("raw_stable") is True, row
            assert row["recording_equal"] is False, row
            assert row["raw"]["bytes"] > 0, row
            if row["stopped"] == "raw_size_limit":
                assert row["raw"]["bytes"] >= report["bounds"]["raw_bytes"], row
            else:
                assert row["wall_ns"] >= report["bounds"]["wall_seconds"] * 1e9, row
            assert "analysis_error" not in row, row
            assert row["heap"]["interpret_exit_code"] == 0, row
            assert row["heap"]["timestamps"]["count"] > 0, row
            assert row["heap"]["coverage"]["undecodable_records"] == 0, row
            status = "bounded"
        elif row["exit_code"] == 1:
            assert row["recording_equal"] is False, row
            assert row["failure"]["message"] and row["stderr_nonempty"], row
            assert row.get("raw_stable", True), row
            status = "run_failed"
        else:
            assert row.get("raw_stable", True), row
            assert row["messages"] == STEPS, row
            assert row["heap"]["interpret_exit_code"] == 0, row
            assert "analysis_error" not in row, row
            assert row["heap"]["coverage"]["undecodable_records"] == 0, row
            assert not row["heap"]["coverage"]["truncated"], row
            assert row["heap"]["known_site"] == {
                "calls": STEPS * ALLOCATIONS_PER_STEP, "sizes": [ALLOCATION_BYTES],
                "expected_calls": STEPS * ALLOCATIONS_PER_STEP, "expected_size": ALLOCATION_BYTES}, row
            status = "completed"
        observations.append({"policy": row["policy"], "mode": row["mode"],
                             "status": status, "run_complete": status == "completed"})
    return observations


def compile_workload(out):
    compiled = []
    for source in ("workload", "preserve"):
        compiled.append(command([os.environ.get("CC", "cc"), "-shared", "-fPIC", "-O2", "-g",
            "-Wall", "-Wextra", "-o", out / f"{source}.so", HERE / f"{source}.c"]))
    assert all(c["exit_code"] == 0 for c in compiled), compiled
    return compiled


def author_manifest(out, policy, *, shimmed=True):
    manifest = Manifest(duration_ns=STEPS * STEP_PERIOD_NS)
    manifest.add_schemas({"proof.Clock": SCHEMA})
    manifest.add_channel("proof.clock", schema="proof.Clock")
    manifest.add_process("process", command=[sys.executable,
        str(HERE / "participant.py"), str(out / "workload.so")],
        step_period_ns=STEP_PERIOD_NS, publishes=["proof.clock"],
        shim=shimmed, **({"sleep": policy} if shimmed else {}))
    name = policy if shimmed else "unshimmed"
    return manifest.write(out / f"{name}.json").path


def capture_run(directory, out, build, tools, path, policy, mode, baseline, raw_limit_bytes):
    profiled = mode != "baseline"
    directory.mkdir()
    recording = directory / "run.mcap"
    raw = directory / "heaptrack.raw"
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    env.update({"SILPROOF_MODE": mode,
        "SILPROOF_HEAPTRACK": str(tools.preload),
        "SILPROOF_PRESERVE": str(out / "preserve.so"),
        "DUMP_HEAPTRACK_OUTPUT": str(raw)})
    runner = "sil-run-diagnostics" if profiled else "sil-run"
    cmd = [build / runner, path, "-o", recording,
           "--participant-timeout-ms", "10000"]
    if profiled:
        cmd += ["--launch-wrapper", "process", HERE / "launch.sh"]
    row = {"policy": policy, "mode": mode, "manifest": digest(path),
           **execute(cmd, directory, env, raw, raw_limit_bytes=raw_limit_bytes)}
    row["stderr_nonempty"] = (directory / "stderr.txt").stat().st_size > 0
    failure = directory / "run.mcap.failure.json"
    if failure.exists():
        row["failure"] = json.loads(failure.read_text())
    if recording.exists():
        row["recording"] = digest(recording)
        try:
            messages = read_messages(recording)
            save(directory / "messages.json", messages)
            row["messages"] = len(messages)
            row["clocks_match_virtual_time"] = {
                field: len(messages) == STEPS and all(
                    m[field] == m["t_ns"] for m in messages)
                for field in ("parent", "child", "grandchild")}
            row["sleep_matches_policy"] = len(messages) == STEPS and all(
                (m["sleep_status"], m["sleep_errno"]) ==
                EXPECTED_SLEEP[policy] for m in messages)
        except Exception as error:
            row["recording_error"] = str(error)
    if profiled:
        row["recording_equal"] = (row["stopped"] is None and row["exit_code"] == 0 and
            recording.exists() and recording.read_bytes() == baseline)
        if raw.exists():
            row["raw"] = digest(raw)
            row["raw_bytes_per_wall_second"] = (
                raw.stat().st_size / (row["wall_ns"] / 1e9))
            try:
                row["heap"] = analyze(raw, tools.interpreter, directory)
            except Exception as error:
                row["analysis_error"] = str(error)
            row["raw_stable"] = digest(raw) == row["raw"]
    return row


def unshimmed_control(out, build, tools, report, raw_limit_bytes):
    """Same allocation/descendant workload with host clocks, without the shim."""
    path = author_manifest(out, "immediate", shimmed=False)
    rows = []
    baseline = None
    messages = []
    for mode in ("baseline", "preload"):
        directory = out / f"unshimmed-{mode}"
        row = capture_run(directory, out, build, tools, path, "immediate", mode,
                          baseline, raw_limit_bytes)
        assert row["exit_code"] == 0 and row["stopped"] is None, row
        assert row["messages"] == STEPS and row["sleep_matches_policy"], row
        decoded = read_messages(directory / "run.mcap")
        assert all(row["host_begin_ns"] <= message[field] <= row["host_end_ns"]
                   for message in decoded for field in ("parent", "child", "grandchild")), row
        # Host-clock payloads are intentionally nondeterministic. Compare the
        # workload results and publication times independently of those fields.
        messages.append([(m["t_ns"], m["result"], m["sleep_status"], m["sleep_errno"])
                         for m in decoded])
        rows.append(row)
        if mode == "baseline":
            baseline = (directory / "run.mcap").read_bytes()
    capture = rows[1]
    assert messages[0] == messages[1], "unshimmed workload results changed"
    assert capture["raw_stable"] and "analysis_error" not in capture, capture
    assert capture["heap"]["coverage"]["undecodable_records"] == 0, capture
    assert not capture["heap"]["coverage"]["truncated"], capture
    assert capture["heap"]["known_site"]["calls"] == STEPS * ALLOCATIONS_PER_STEP, capture
    assert capture["heap"]["known_site"]["sizes"] == [ALLOCATION_BYTES], capture
    assert capture["heap"]["timestamps"]["count"] > 0, capture
    comparisons = [{"policy": r["policy"], "mode": r["mode"],
                    "raw_bytes_ratio": r["raw"]["bytes"] / capture["raw"]["bytes"],
                    "timestamp_records_ratio": (r["heap"]["timestamps"]["count"] /
                                                capture["heap"]["timestamps"]["count"])}
                   for r in report["runs"] if r["mode"] != "baseline"
                   and r["stopped"] is None and r["exit_code"] == 0]
    return {"runs": rows, "non_clock_fields_equal": True, "comparisons": comparisons,
            "scope": "single captures of this workload; not an overhead benchmark"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--allow-other-machine", action="store_true",
                        help="smoke only; never qualification evidence")
    parser.add_argument("--raw-limit-bytes", type=int, default=CAP_BYTES,
                        help="raw-file stop threshold; lower it to exercise bounded capture")
    parser.add_argument("--unshimmed-control", action="store_true",
                        help="also capture the same workload without the Clock shim")
    args = parser.parse_args()
    if args.raw_limit_bytes <= 0:
        parser.error("--raw-limit-bytes must be positive")
    qualified = (platform.system() == "Linux" and platform.machine() == "x86_64"
                 and platform.libc_ver()[0] == "glibc")
    if not qualified and not args.allow_other_machine:
        parser.error("qualification requires Linux x86-64/glibc")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    build = args.build.resolve()
    tools = find_heaptrack()
    compiled = compile_workload(out)
    report = {"issue": 330, "qualified_machine": qualified, "complete": False,
        "host": {"system": platform.platform(), "machine": platform.machine(),
                 "libc": platform.libc_ver(), "uid": os.getuid()},
        "tools": [command(["heaptrack", "--version"]), command([os.environ.get("CC", "cc"), "--version"]),
                  command(["dpkg-query", "-W", "heaptrack", "libc6", "libstdc++6"]),
                  command(["git", "-C", HERE, "rev-parse", "HEAD"])],
        "build_commands": compiled,
        "artifacts": {str(p): digest(p) for p in (
            tools.preload, tools.interpreter, build / "sil-run", build / "sil-run-diagnostics",
            build / "libsil_clock_shim.so", out / "preserve.so", out / "workload.so")},
        "bounds": {"wall_seconds": WALL_LIMIT_SECONDS, "raw_bytes": args.raw_limit_bytes}, "runs": []}
    save(out / "results.json", report)
    for policy in ("reject", "immediate"):
        path = author_manifest(out, policy)
        baseline = None
        for mode in ("baseline", "preload", "preserve"):
            row = capture_run(out / f"{policy}-{mode}", out, build, tools, path,
                              policy, mode, baseline, args.raw_limit_bytes)
            report["runs"].append(row)
            save(out / "results.json", report)
            if mode == "baseline":
                assert row["exit_code"] == 0 and row["messages"] == STEPS, row
                assert all(row["clocks_match_virtual_time"].values()), row
                assert row["sleep_matches_policy"], row
                baseline = (out / f"{policy}-{mode}" / "run.mcap").read_bytes()
            print(policy, mode, row["exit_code"], row.get("recording_equal"), flush=True)
    try:
        report["observations"] = validate_evidence(report)
        if args.unshimmed_control:
            report["unshimmed_control"] = unshimmed_control(
                out, build, tools, report, args.raw_limit_bytes)
        report["complete"] = True
    finally:
        save(out / "results.json", report)
        # Preserve an inventory even when collection validation fails.
        save(out / "digests.json", {str(p.relative_to(out)): digest(p)
            for p in sorted(out.rglob("*")) if p.is_file()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
