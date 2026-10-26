"""Disposable #330 experiment; no production profiling interface changes.

Run on Linux x86-64/glibc with a fresh output directory. Failed hypotheses
are observations. Only workload integrity and evidence completeness gate it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

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
CAP_BYTES = 128 * 1024 * 1024


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def digest(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size}


def command(args):
    done = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                          timeout=60)
    return {"command": list(map(str, args)), "exit_code": done.returncode,
            "stdout": done.stdout, "stderr": done.stderr}


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
                if time.monotonic_ns() - began > 30_000_000_000:
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
        "clock": "steady_clock intercepted by Clock shim; initialization origin may precede shim mapping; see candidates.md",
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
        "expected_calls": STEPS * 8, "expected_size": 2048}
    result["coverage"] = analysis.record["coverage"]
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
                "calls": 80, "sizes": [2048], "expected_calls": 80,
                "expected_size": 2048}, row
            status = "completed"
        observations.append({"policy": row["policy"], "mode": row["mode"],
                             "status": status, "run_complete": status == "completed"})
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--allow-other-machine", action="store_true",
                        help="smoke only; never qualification evidence")
    parser.add_argument("--raw-limit-bytes", type=int, default=CAP_BYTES,
                        help="raw-file stop threshold; lower it to exercise bounded capture")
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
    compiled = []
    for source in ("workload", "preserve"):
        compiled.append(command([os.environ.get("CC", "cc"), "-shared", "-fPIC", "-O2", "-g",
            "-Wall", "-Wextra", "-o", out / f"{source}.so", HERE / f"{source}.c"]))
    assert all(c["exit_code"] == 0 for c in compiled), compiled
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
        "bounds": {"wall_seconds": 30, "raw_bytes": args.raw_limit_bytes}, "runs": []}
    save(out / "results.json", report)
    for policy in ("reject", "immediate"):
        manifest = Manifest(duration_ns=STEPS * 100_000_000)
        manifest.add_schemas({"proof.Clock": SCHEMA})
        manifest.add_channel("proof.clock", schema="proof.Clock")
        manifest.add_process("process", command=[sys.executable,
            str(HERE / "participant.py"), str(out / "workload.so")],
            step_period_ns=100_000_000, publishes=["proof.clock"],
            shim=True, sleep=policy)
        path = manifest.write(out / f"{policy}.json").path
        baseline = None
        for mode in ("baseline", "preload", "preserve"):
            directory = out / f"{policy}-{mode}"
            directory.mkdir()
            recording = directory / "run.mcap"
            raw = directory / "heaptrack.raw"
            env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
            env.update({"SILPROOF_MODE": mode,
                "SILPROOF_HEAPTRACK": str(tools.preload),
                "SILPROOF_PRESERVE": str(out / "preserve.so"),
                "DUMP_HEAPTRACK_OUTPUT": str(raw)})
            runner = "sil-run" if mode == "baseline" else "sil-run-diagnostics"
            cmd = [build / runner, path, "-o", recording,
                   "--participant-timeout-ms", "10000"]
            if mode != "baseline":
                cmd += ["--launch-wrapper", "process", HERE / "launch.sh"]
            row = {"policy": policy, "mode": mode, "manifest": digest(path),
                   **execute(cmd, directory, env, raw, raw_limit_bytes=args.raw_limit_bytes)}
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
                        ((-1, 38) if policy == "reject" else (0, 0)) for m in messages)
                except Exception as error:
                    row["recording_error"] = str(error)
            if mode == "baseline":
                assert row["exit_code"] == 0 and row["messages"] == STEPS, row
                assert all(row["clocks_match_virtual_time"].values()), row
                assert row["sleep_matches_policy"], row
                baseline = recording.read_bytes()
            else:
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
            report["runs"].append(row)
            save(out / "results.json", report)
            print(policy, mode, row["exit_code"], row.get("recording_equal"), flush=True)
    try:
        report["observations"] = validate_evidence(report)
        report["complete"] = True
    finally:
        save(out / "results.json", report)
        # Preserve an inventory even when collection validation fails.
        save(out / "digests.json", {str(p.relative_to(out)): digest(p)
            for p in sorted(out.rglob("*")) if p.is_file()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
