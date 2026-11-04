"""Issue #362: run a bounded scheduler-event experiment, never a live Run control API."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import signal
import shutil
import struct
import sys

from capture import run_bounded_tool
from session import ExperimentCase, RunObservation
from evidence import PidNamespace, sha256, write_json
from decode import bind_capture

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


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
        result = run_bounded_tool(argv, work / f"build-{i}.txt", timeout=600)
        results.append(result)
        write_json(work / "commands.json", results)
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


def runner_arguments(work, out, case):
    runner = "sil-run-diagnostics" if case.observed else "sil-run"
    argv = [str(work / "build" / runner), str(manifest(work, case.mode)),
            "-o", str(out / "run.mcap"), "--participant-timeout-ms", str(case.deadline_ms)]
    if case.observed:
        argv += ["--diagnostics-out", str(out / "diagnostics.json"),
                 "--activation-observations", str(out / "activations.jsonl"),
                 "--activation-observations-limit-bytes", str(1 << 20)]
    return argv


def observe_fixture_run(work, out, case):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "python/src"),
           "SIL_SCHED_MARKERS": str(out / "markers.jsonl")}
    result = RunObservation(out, case).observe(runner_arguments(work, out, case), env)
    if (out / "run.mcap").exists():
        result["recording_sha256"] = sha256(out / "run.mcap")
        if case.mode != "unclosed":
            result["messages"] = recorded_messages(out / "run.mcap")
    return result


def recorded_messages(recording):
    from sil.recording import read_records
    messages = []
    for channel, at, data in read_records(recording):
        index, value = struct.unpack("<QQ", data)
        messages.append({"channel": channel, "virtual_ns": at, "index": index, "value": value})
    return messages


def export(out, result):
    raw = out / "perf.data"
    if result["fault"] == "truncated":
        with raw.open("r+b") as stream:
            stream.truncate(raw.stat().st_size // 2)
        result["injected_truncation"] = True
    exports = {}
    exports["events"] = run_bounded_tool(["perf", "script", "--ns", "-i", raw,
        "-F", "cpu,time,event,trace"], out / "events.txt", stderr=out / "events-errors.txt")
    exports["attributes"] = run_bounded_tool(["perf", "evlist", "-v", "-i", raw], out / "attributes.txt")
    exports["stats"] = run_bounded_tool(["perf", "script", "-D", "-i", raw], out / "stats.txt")
    result["exports"] = exports
    result["raw_bytes"] = raw.stat().st_size


def qualify_environment(work, out, environment, initial_namespace):
    out.mkdir(parents=True, exist_ok=False)
    ns = PidNamespace.current()
    record_environment(work, out, environment, ns, initial_namespace)
    rows = []
    for case in experiment_cases():
        directory = out / case.name
        result = observe_fixture_run(work, directory, case)
        result.update(name=case.name, initial_pid_namespace=ns == initial_namespace)
        if case.observed and result["availability"] == "available":
            export(directory, result)
            document, analysis = bind_capture(directory, result, ns)
            write_json(directory / "input.json", document)
            write_json(directory / "analysis.json", analysis)
        write_json(directory / "result.json", result)
        rows.append(result)
        write_json(out / "results.json", rows)
        if result["availability"] == "unavailable":
            break
    seal_and_validate(out)


def record_environment(work, out, environment, ns, initial_namespace):
    facts = {"environment": environment, "system": platform.system(),
             "machine": platform.machine(), "kernel": platform.release(),
             "uid": os.getuid(), "cpu_count": os.cpu_count(), "pid_namespace": ns.as_pair(),
             "initial_pid_namespace": initial_namespace.as_pair(), "source_revision": os.environ.get("SIL_SOURCE_REVISION")}
    facts.update(host_policy())
    record_tool_identities(work, out, facts)
    record_event_formats(out)
    record_tool_notices(out)
    write_json(out / "identity.json", facts)


def host_policy():
    facts = {}
    for name, path in (("status", "/proc/self/status"), ("paranoid", "/proc/sys/kernel/perf_event_paranoid"),
                       ("bpf_disabled", "/proc/sys/kernel/unprivileged_bpf_disabled"), ("mounts", "/proc/mounts")):
        try:
            facts[name] = Path(path).read_text()
        except OSError as exc:
            facts[name] = str(exc)
    return facts


def record_tool_identities(work, out, facts):
    facts["commands"] = [run_bounded_tool(argv, out / f"identity-{i}.txt") for i, argv in enumerate((
        ["perf", "version", "--build-options"], ["cc", "--version"], ["ldd", "--version"],
        ["dpkg-query", "-W"], ["readelf", "-n", work / "workload.so"]))]
    facts["binaries"] = {str(p): sha256(p) for p in (
        work / "workload.so", work / "exec-helper", work / "build/sil-run", work / "build/sil-run-diagnostics")}
    tools = [Path(found).resolve() for name in ("perf", "cc") if (found := shutil.which(name))]
    tools += list(Path("/usr/lib").glob("linux-tools*/**/perf"))
    facts["tool_binaries"] = {str(p): sha256(p) for p in tools if p.is_file()}
    facts["sources"] = {p.name: sha256(p) for p in HERE.iterdir() if p.is_file()}
    facts["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def record_event_formats(out):
    formats = out / "event-formats"
    formats.mkdir()
    from capture import EVENTS
    for event in EVENTS:
        try:
            (formats / f"{event}.txt").write_text(Path(f"/sys/kernel/tracing/events/sched/{event}/format").read_text())
        except OSError as exc:
            (formats / f"{event}.txt").write_text(str(exc))


def record_tool_notices(out):
    notices = out / "licenses"
    notices.mkdir()
    for notice in Path("/usr/share/doc").glob("linux-tools*/copyright"):
        (notices / f"{notice.parent.name}.copyright").write_bytes(notice.read_bytes())


def experiment_cases():
    cases = [("success", i, None) for i in range(3)] + [("failure", 0, None), ("unclosed", 0, None)]
    cases += [("edge", i, None) for i in range(3)]
    cases += [("success", 0, fault) for fault in ("interrupt", "limit", "loss", "truncated")]
    return [ExperimentCase(mode, index, observed, fault) for mode, index, fault in cases
            for observed in (False, True) if observed or fault is None]


def seal_and_validate(out):
    write_json(out / "integrity.json", {str(p.relative_to(out)): sha256(p) for p in sorted(out.rglob("*")) if p.is_file()})
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
        qualify_environment(args.work.resolve(), args.out.resolve(), args.environment,
                            PidNamespace.from_argument(args.initial_pid_namespace))
