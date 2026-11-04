"""Issue #362: run a bounded scheduler-event experiment, never a live Run control API."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import struct
import sys
import time

from capture import FILE_BYTES, GUARD_BYTES, GUARD_SECONDS, Perf, command
from decode import prepare

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def namespace(path="/proc/self/ns/pid"):
    ns = os.stat(path)
    return [ns.st_dev, ns.st_ino]


def build(work):
    work.mkdir(parents=True, exist_ok=False)
    commands = [
        ["cmake", "-S", ROOT, "-B", work / "build", "-DCMAKE_BUILD_TYPE=Release"],
        ["cmake", "--build", work / "build", "--target", "sil-run", "sil-run-diagnostics", "-j2"],
        ["cc", "-O2", "-g", "-fno-omit-frame-pointer", "-Wall", "-Wextra", "-Werror",
         "-shared", "-fPIC", "-pthread", HERE / "workload.c", "-o", work / "workload.so"],
        ["cc", "-O2", "-g", "-fno-omit-frame-pointer", "-Wall", "-Wextra", "-Werror",
         "-pthread", HERE / "workload.c", "-o", work / "exec-helper"],
    ]
    results = []
    for i, argv in enumerate(commands):
        result = command(argv, work / f"build-{i}.txt", timeout=600)
        results.append(result)
        write(work / "commands.json", results)
        if result["exit_code"]:
            raise RuntimeError(f"build failed: {result}")


def manifest(work, mode):
    from sil.manifest import Manifest
    path = work / f"{mode}.json"
    if path.exists():
        return path
    m = Manifest(duration_ns=3_000_000)
    m.add_schemas({"proof.Scheduler": {"fields": [
        {"name": "index", "type": "u64"}, {"name": "value", "type": "u64"}]}})
    for name in ("slow", "peer"):
        m.add_channel(name, schema="proof.Scheduler")
        m.add_process(name, command=[sys.executable, str(HERE / "participant.py"),
            str(work / "workload.so"), str(work / "exec-helper"), name,
            mode if name == "slow" else "success"], step_period_ns=1_000_000, publishes=[name])
    m.write(path)
    return path


def execute(work, out, mode, observed, fault):
    out.mkdir()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "python/src"),
           "SIL_SCHED_MARKERS": str(out / "markers.jsonl")}
    deadline_ms = 180 if mode == "edge" else 10000
    argv = [str(work / "build" / ("sil-run-diagnostics" if observed else "sil-run")),
            str(manifest(work, mode)), "-o", str(out / "run.mcap"),
            "--participant-timeout-ms", str(deadline_ms)]
    if observed:
        argv += ["--diagnostics-out", str(out / "diagnostics.json"),
                 "--activation-observations", str(out / "activations.jsonl"),
                 "--activation-observations-limit-bytes", str(1 << 20)]
    perf = Perf(out, pages="1" if fault == "loss" else "1M") if observed else None
    result = {"mode": mode, "fault": fault, "observed": observed,
              "policies": {"response_deadline_ms": deadline_ms, "guard_seconds": GUARD_SECONDS,
                  "raw_guard_bytes": GUARD_BYTES, "hard_per_file_bytes": FILE_BYTES,
                  "poll_ms": 10, "sampling_subtraction": False}, "growth": []}
    if perf and not perf.ready:
        result["collector"] = perf.finish()
        result["availability"] = "unavailable"
        write(out / "result.json", result)
        return result
    start = time.monotonic_ns()
    stopped, collector_stopped, paused_at = None, False, None
    with (out / "stdout.txt").open("wb") as stdout, (out / "stderr.txt").open("wb") as stderr:
        try:
            process = subprocess.Popen(argv, cwd=out, env=env, stdout=stdout,
                                       stderr=stderr, start_new_session=True)
        except BaseException:
            if perf:
                perf.finish()
            raise
        try:
            while process.poll() is None:
                elapsed = time.monotonic_ns() - start
                raw = out / "perf.data"
                size = raw.stat().st_size if raw.exists() else 0
                result["growth"].append([elapsed, size])
                if elapsed > GUARD_SECONDS * 10**9:
                    stopped = "wall_guard"
                    break
                if perf and not collector_stopped:
                    markers = (out / "markers.jsonl").read_text() if (out / "markers.jsonl").exists() else ""
                    if fault == "loss" and paused_at is None and '"phase":"cpu"' in markers:
                        perf.process.send_signal(signal.SIGSTOP)
                        paused_at = elapsed
                    if paused_at is not None and elapsed - paused_at >= 100_000_000:
                        perf.process.send_signal(signal.SIGCONT)
                    if size > GUARD_BYTES or (fault == "limit" and size > 65536) or (
                            fault == "interrupt" and '"phase":"sleep"' in markers):
                        result["collector_stop_reason"] = "size_guard" if fault != "interrupt" else "interrupted"
                        result["collector"] = perf.finish()
                        collector_stopped = True
                time.sleep(.01)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            run_end = time.monotonic_ns()
            if perf and not collector_stopped:
                perf.process.send_signal(signal.SIGCONT) if perf.process.poll() is None else None
                result["collector"] = perf.finish()
    result["run"] = {"argv": argv, "exit_code": process.returncode, "stopped": stopped,
                     "wall_ns": run_end - start}
    result["availability"] = "available" if perf else "not_requested"
    if (out / "run.mcap").exists():
        result["recording_sha256"] = digest(out / "run.mcap")
        if mode != "unclosed":
            from sil.recording import read_records
            result["messages"] = [{"channel": channel, "virtual_ns": at,
                "index": struct.unpack("<QQ", data)[0], "value": struct.unpack("<QQ", data)[1]}
                for channel, at, data in read_records(out / "run.mcap")]
    return result


def export(out, result):
    raw = out / "perf.data"
    if result["fault"] == "truncated":
        with raw.open("r+b") as stream:
            stream.truncate(raw.stat().st_size // 2)
        result["injected_truncation"] = True
    exports = {}
    exports["events"] = command(["perf", "script", "--ns", "-i", raw,
        "-F", "cpu,time,event,trace"], out / "events.txt", stderr=out / "events-errors.txt")
    exports["attributes"] = command(["perf", "evlist", "-v", "-i", raw], out / "attributes.txt")
    exports["stats"] = command(["perf", "script", "-D", "-i", raw], out / "stats.txt")
    result["exports"] = exports
    result["raw_bytes"] = raw.stat().st_size


def run(work, out, environment, initial_namespace):
    out.mkdir(parents=True, exist_ok=False)
    ns = namespace()
    facts = {"environment": environment, "system": platform.system(),
             "machine": platform.machine(), "kernel": platform.release(),
             "uid": os.getuid(), "cpu_count": os.cpu_count(), "pid_namespace": ns,
             "initial_pid_namespace": initial_namespace, "source_revision": os.environ.get("SIL_SOURCE_REVISION")}
    for name, path in (("status", "/proc/self/status"), ("paranoid", "/proc/sys/kernel/perf_event_paranoid"),
                       ("bpf_disabled", "/proc/sys/kernel/unprivileged_bpf_disabled"), ("mounts", "/proc/mounts")):
        try:
            facts[name] = Path(path).read_text()
        except OSError as exc:
            facts[name] = str(exc)
    facts["commands"] = [command(argv, out / f"identity-{i}.txt") for i, argv in enumerate((
        ["perf", "version", "--build-options"], ["cc", "--version"], ["ldd", "--version"],
        ["dpkg-query", "-W"], ["readelf", "-n", work / "workload.so"]))]
    facts["binaries"] = {str(p): digest(p) for p in (
        work / "workload.so", work / "exec-helper", work / "build/sil-run", work / "build/sil-run-diagnostics")}
    tools = [Path(found).resolve() for name in ("perf", "cc") if (found := shutil.which(name))]
    tools += list(Path("/usr/lib").glob("linux-tools*/**/perf"))
    facts["tool_binaries"] = {str(p): digest(p) for p in tools if p.is_file()}
    facts["sources"] = {p.name: digest(p) for p in HERE.iterdir() if p.is_file()}
    facts["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    formats = out / "event-formats"
    formats.mkdir()
    from capture import EVENTS
    for event in EVENTS:
        try:
            (formats / f"{event}.txt").write_text(Path(f"/sys/kernel/tracing/events/sched/{event}/format").read_text())
        except OSError as exc:
            (formats / f"{event}.txt").write_text(str(exc))
    write(out / "identity.json", facts)
    rows = []
    cases = [("success", i, None) for i in range(3)] + [("failure", 0, None), ("unclosed", 0, None)]
    cases += [("edge", i, None) for i in range(3)]
    cases += [("success", 0, fault) for fault in ("interrupt", "limit", "loss", "truncated")]
    for mode, index, fault in cases:
        for observed in (False, True):
            if fault and not observed:
                continue
            name = f"{mode}-{index}-{'observed' if observed else 'ordinary'}-{fault or 'normal'}"
            directory = out / name
            result = execute(work, directory, mode, observed, fault)
            result.update(name=name, initial_pid_namespace=ns == initial_namespace)
            if observed and result["availability"] == "available":
                export(directory, result)
                document, analysis = prepare(directory, result, ns)
                write(directory / "input.json", document)
                write(directory / "analysis.json", analysis)
            write(directory / "result.json", result)
            rows.append(result)
            write(out / "results.json", rows)
            if result["availability"] == "unavailable":
                seal(out)
                return
    seal(out)


def seal(out):
    write(out / "integrity.json", {str(p.relative_to(out)): digest(p) for p in sorted(out.rglob("*")) if p.is_file()})
    from validate import validate
    validate(out)


if __name__ == "__main__":
    def interrupt(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt(f"proof interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "run"))
    parser.add_argument("work", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--environment")
    parser.add_argument("--initial-pid-namespace", help="device:inode from the native host's /proc/1/ns/pid")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("qualification requires Linux x86-64/glibc")
    if args.action == "build":
        build(args.work.resolve())
    else:
        if args.out is None or args.environment is None or args.initial_pid_namespace is None:
            parser.error("run requires --out, --environment and --initial-pid-namespace")
        run(args.work.resolve(), args.out.resolve(), args.environment,
            [int(value) for value in args.initial_pid_namespace.split(":")])
