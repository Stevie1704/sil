#!/usr/bin/env python3
"""Observational cost of run diagnostics: `sil-run` against `sil-run-diagnostics`.

Runs each workload `--pairs` times as an alternating pair: once with the
ordinary runner, once with the diagnostics runner. Both are started through
`sil-diagnostics-launcher`, so both pay the same launcher cost and no runner
inherits this script's peak RSS. Per Run it keeps the host wall time around
the launcher process, the largest-process peak RSS that `wait4` returns for
the launcher and its waited descendants, the exit code and a hash of the
Recording. The two runners must write the same Recording bytes.

    tools/diagnostics_overhead.py --build-dir build --machine "<name>"

Writes `diagnostics-overhead.json` (raw) and `diagnostics-overhead-results.md`
into docs/bench/; `diagnostics-overhead.md` there explains them. The values
describe one machine and build. They are not a
correction to subtract from a measured Run, and not a promise of uninstrumented
performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_determinism import full_pipeline_manifest  # noqa: E402
from toys import toy_manifest  # noqa: E402

MS = 1_000_000
# Linux reports ru_maxrss in KiB, macOS in bytes.
RSS_UNIT = 1 if sys.platform == "darwin" else 1024


def multitask_manifest(directory: Path, build: Path, duration_s: int) -> Path:
    """Two Native Tasks, 1 ms and 5 ms: 1,200 activations per virtual second."""
    m = toy_manifest(duration_ns=duration_s * 1000 * MS)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_native(
        "multi",
        library=str(build / "toy_multitask.silp"),
        config={"channel": "ticks", "fast_period_ns": 1 * MS,
                "slow_period_ns": 5 * MS, "seed": 7},
        publishes=["ticks"],
    )
    return m.write(directory / f"multitask-{duration_s}s.json").path


def pipeline_manifest(directory: Path, duration_s: int) -> Path:
    """Native producer and accumulator plus a Python Process echo at 10 ms."""
    path = full_pipeline_manifest(directory).path
    document = json.loads(path.read_text())
    document["duration_ns"] = duration_s * 1000 * MS
    target = directory / f"pipeline-{duration_s}s.json"
    target.write_text(json.dumps(document, indent=2) + "\n")
    return target


def workloads(directory: Path, build: Path) -> dict[str, tuple[str, Path]]:
    return {
        "native-multitask-100s": (
            "Native participant, 2 Tasks (1 ms, 5 ms), 100 s Duration: "
            "120,000 activations", multitask_manifest(directory, build, 100)),
        "native-multitask-1000s": (
            "the same Participant, 1,000 s Duration: 1,200,000 activations, "
            "far past the 8,192-sample reservoir",
            multitask_manifest(directory, build, 1000)),
        "native-and-process-60s": (
            "2 Native Tasks (10 ms, 20 ms) and a Python Process participant "
            "(10 ms Step), 60 s Duration: 6,000 Process Steps",
            pipeline_manifest(directory, 60)),
    }


def measure(launcher: Path, runner: Path, manifest: Path, work: Path,
            diagnostics: bool) -> dict:
    recording = work / "run.mcap"
    report = work / "run.json"
    command = [str(launcher), str(runner), str(manifest), "-o", str(recording)]
    if diagnostics:
        command += ["--diagnostics-out", str(report)]
    started = time.monotonic_ns()
    process = subprocess.Popen(command, cwd=work, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
    _, status, usage = os.wait4(process.pid, 0)
    wall_ns = time.monotonic_ns() - started
    stderr = process.stderr.read().decode(errors="replace")
    process.stderr.close()
    result = {
        "exit_code": os.waitstatus_to_exitcode(status),
        "external_wall_ns": wall_ns,
        "tree_largest_process_peak_rss_bytes": usage.ru_maxrss * RSS_UNIT,
        "recording_sha256": hashlib.sha256(recording.read_bytes()).hexdigest(),
    }
    if result["exit_code"] != 0:
        raise SystemExit(f"{runner.name} failed on {manifest.name}: {stderr}")
    if diagnostics:
        document = json.loads(report.read_text())
        observational = document["observational"]
        result["report"] = {
            "run_wall_ns": observational["run_wall_time"]["wall_ns"],
            "scheduled_execution_ns":
                observational["phases"]["scheduled_execution_ns"],
            "runner_peak_rss_bytes":
                observational["resources"]["runner"]["peak_rss_bytes"],
            "tasks": [
                {"participant": row["participant"], "task": row["task"],
                 "completed": row["timing"]["completed"],
                 "retained_count": row["timing"]["quantiles"]["retained_count"],
                 "approximate": row["timing"]["quantiles"]["approximate"]}
                for row in observational["tasks"]
            ],
        }
        report.unlink()
    recording.unlink()
    for side_car in work.glob("run.mcap.*"):
        side_car.unlink()
    return result


def spread(values: list[int]) -> dict:
    return {"min": min(values), "median": statistics.median(values),
            "max": max(values), "count": len(values)}


def summarize(runs: list[dict]) -> dict:
    by_mode = {mode: [r for r in runs if r["mode"] == mode]
               for mode in ("ordinary", "diagnostics")}
    summary = {
        mode: {
            "external_wall_ns": spread([r["external_wall_ns"] for r in rows]),
            "tree_largest_process_peak_rss_bytes": spread(
                [r["tree_largest_process_peak_rss_bytes"] for r in rows]),
        }
        for mode, rows in by_mode.items()
    }
    summary["median_wall_ratio"] = (
        summary["diagnostics"]["external_wall_ns"]["median"]
        / summary["ordinary"]["external_wall_ns"]["median"])
    summary["recordings_identical"] = len(
        {r["recording_sha256"] for r in runs}) == 1
    return summary


def host_identity(machine: str) -> dict:
    cpu = None
    if sys.platform == "darwin":
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True).stdout.strip()
    elif Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    return {"machine": machine, "os": platform.platform(),
            "architecture": platform.machine(), "cpu": cpu or None,
            "logical_cpus": os.cpu_count()}


def build_identity(build: Path) -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True).stdout.strip()
    build_type = None
    cache = build / "CMakeCache.txt"
    if cache.exists():
        for line in cache.read_text().splitlines():
            if line.startswith("CMAKE_BUILD_TYPE:"):
                build_type = line.split("=", 1)[1] or None
    version = subprocess.run([str(build / "sil-run"), "--version"],
                             capture_output=True, text=True).stdout.strip()
    return {"source_revision": git("rev-parse", "HEAD"),
            "worktree_clean": git("status", "--porcelain") == "",
            "cmake_build_type": build_type, "sil_version": version}


def markdown(document: dict) -> str:
    def ms(ns: float) -> str:
        return f"{ns / MS:.1f}"

    def mib(n: float) -> str:
        return f"{n / 2**20:.1f}"

    host, build = document["host"], document["build"]
    lines = [
        "# Run diagnostics overhead (generated)",
        "",
        "Generated by `tools/diagnostics_overhead.py`; "
        "[diagnostics-overhead.md](diagnostics-overhead.md) explains it.",
        "",
        f"- Machine: {host['machine']}; {host['cpu']}; {host['logical_cpus']} "
        f"logical CPUs; {host['os']} ({host['architecture']})",
        f"- Build: SiL {build['sil_version']}, revision "
        f"`{build['source_revision']}`"
        f"{'' if build['worktree_clean'] else ' plus uncommitted changes'}, "
        f"CMake `{build['cmake_build_type']}`",
        f"- Pairs per workload: {document['pairs']}, ordinary and diagnostics "
        "alternating, both through `sil-diagnostics-launcher`, Recording on",
        "",
        "| Workload | Runner | Wall min / median / max (ms) | "
        "Largest-process peak RSS median (MiB) |",
        "| --- | --- | --- | --- |",
    ]
    for name, workload in document["workloads"].items():
        for mode in ("ordinary", "diagnostics"):
            wall = workload["summary"][mode]["external_wall_ns"]
            rss = workload["summary"][mode]["tree_largest_process_peak_rss_bytes"]
            lines.append(
                f"| {name} | {mode} | {ms(wall['min'])} / {ms(wall['median'])}"
                f" / {ms(wall['max'])} | {mib(rss['median'])} |")
    lines += ["", "| Workload | Median wall ratio (diagnostics / ordinary) | "
              "Recordings identical | Runner peak RSS in report, median (MiB) "
              "| Retained samples per Task |",
              "| --- | --- | --- | --- | --- |"]
    for name, workload in document["workloads"].items():
        reports = [r["report"] for r in workload["runs"] if "report" in r]
        runner_rss = statistics.median(r["runner_peak_rss_bytes"] for r in reports)
        retained = ", ".join(
            f"{t['participant']}/{t['task']} {t['retained_count']} of "
            f"{t['completed']}{' (approximate)' if t['approximate'] else ''}"
            for t in reports[0]["tasks"])
        lines.append(
            f"| {name} | {workload['summary']['median_wall_ratio']:.3f} | "
            f"{'yes' if workload['summary']['recordings_identical'] else 'NO'}"
            f" | {mib(runner_rss)} | {retained} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build")
    parser.add_argument("--machine", required=True,
                        help="a name for the machine, written into the evidence")
    parser.add_argument("--pairs", type=int, default=20)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "bench")
    args = parser.parse_args()
    build = args.build_dir.resolve()
    launcher = build / "sil-diagnostics-launcher"
    runners = {"ordinary": build / "sil-run",
               "diagnostics": build / "sil-run-diagnostics"}

    document = {"sil_diagnostics_overhead": 1, "host": host_identity(args.machine),
                "build": build_identity(build), "pairs": args.pairs,
                "workloads": {}}
    with tempfile.TemporaryDirectory(prefix="sil-overhead-") as scratch:
        scratch = Path(scratch)
        for name, (description, manifest) in workloads(scratch, build).items():
            work = scratch / name
            work.mkdir()
            runs = []
            for index in range(args.pairs):
                for mode, runner in runners.items():
                    run = measure(launcher, runner, manifest, work,
                                  mode == "diagnostics")
                    runs.append({"mode": mode, "pair": index + 1, **run})
            document["workloads"][name] = {
                "description": description, "runs": runs,
                "summary": summarize(runs)}
            if not document["workloads"][name]["summary"]["recordings_identical"]:
                raise SystemExit(f"{name}: the runners wrote different Recordings")
            print(f"{name}: median wall ratio "
                  f"{document['workloads'][name]['summary']['median_wall_ratio']:.3f}",
                  file=sys.stderr)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "diagnostics-overhead.json").write_text(
        json.dumps(document, indent=2) + "\n")
    (args.out_dir / "diagnostics-overhead-results.md").write_text(
        markdown(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
