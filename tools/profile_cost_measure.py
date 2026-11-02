#!/usr/bin/env python3
"""Runner work and profile finalization of `sil profile`, kept apart (#329).

Each repetition is one fresh `sil profile` command in its own process. A small
driver counts each hash of the profile tool. The parent reads the runner state
from the side-car. A faster Run does not make a faster command if the work moved
to finalization, so this tool reports the end-to-end time as well.

    tools/profile_cost_measure.py --revision <commit> --mode default \\
        --machine "<name>" --load-condition "<what else runs>" --label <name>

Without real tools the tool uses the fake perf of the tests. That is a measure
of the runner and the finalization only: it samples nothing, so it has no perf
overhead and only a small export. Use `--real-tools` on the supported machine.
No test or gate reads a duration from the evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from hash_baseline import (  # noqa: E402
    _cache_entries, _multitask_manifest, _toy_manifest, build_revision,
    host_identity,
    select_build,
)

MS = 1_000_000
NATIVE = ("sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher")
CASES = {
    "cpu": ("native", ["--collector", "cpu"]),
    "cpu-activations": ("native", ["--collector", "cpu", "--activations"]),
    "heap-runner": ("native", ["--collector", "heap", "--runner"]),
    "heap-process": ("process", ["--collector", "heap", "--participant",
                                 "worker"]),
}
FAKE_STATEMENT = (
    "The fake perf of tests/fake_perf.py does not sample. Its capture and its "
    "export are small, so post-Run processing here is far below that of perf.")
REAL_STATEMENT = "The real collector on PATH, as the capture records."
RUNNER_END = (
    "Modification time of the side-car, the last file of the runner. It is "
    "on the same clock as the start of the command, with file-system "
    "resolution. The end of perf or Heaptrack is in post-Run time.")
# Runs in the source tree under test, with its own profile_common.
DRIVER = """
import hashlib, json, os, sys, time
from pathlib import Path
from sil import profile_common, profile_run
rows = []
real = profile_common.file_sha256
def counted(path):
    begin = time.perf_counter_ns()
    digest = real(path)
    rows.append({"path": os.path.realpath(path),
                 "bytes": os.path.getsize(path), "sha256": digest,
                 "ns": time.perf_counter_ns() - begin})
    return digest
profile_common.file_sha256 = counted
start = time.time_ns()
try:
    code = profile_run.main(sys.argv[2:])
except SystemExit as stop:
    code = stop.code
Path(sys.argv[1]).write_text(json.dumps(
    {"exit_code": code, "start_ns": start, "end_ns": time.time_ns(),
     "hashes": rows}))
"""


def split_phases(start_ns: int, runner_end_ns: int, end_ns: int,
                 profile_hash_ns: int) -> dict:
    """Split one command at the end of the runner."""
    if not start_ns <= runner_end_ns <= end_ns:
        raise ValueError("the runner end is outside the command")
    post_run = end_ns - runner_end_ns
    return {"end_to_end_ns": end_ns - start_ns,
            "capture_and_run_ns": runner_end_ns - start_ns,
            "post_run_ns": post_run,
            "profile_hash_ns": profile_hash_ns,
            "post_run_without_hash_ns": post_run - profile_hash_ns}


def verify_profile_hashes(rows: list[dict], directory: Path) -> None:
    """Each digest of the profile tool must match an independent SHA-256."""
    for row in rows:
        path = directory / row["path"]
        with open(path, "rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if actual != row["sha256"]:
            raise ValueError(f"profile digest of {path} does not match")


def input_digests(side_car: dict) -> dict:
    """The input digests that the runner calculated, as the side-car says."""
    artifacts = side_car["artifacts"]
    entries = [artifacts["runner"], artifacts.get("clock_shim"),
               *(row["library"] for row in artifacts["native_participants"]),
               *(row["executable"] for row in artifacts["process_participants"]),
               *(row["file"] for row in artifacts["command_files"])]
    # Schema 1 has no state: a string digest is a calculated one.
    calculated = [entry for entry in entries
                  if entry and isinstance(entry.get("sha256"), str)]
    distinct = {entry["path"] for entry in calculated}
    return {"entries": len(calculated), "distinct_paths": len(distinct),
            "distinct_bytes": sum(os.path.getsize(path) for path in distinct)}


def recording_state(side_car: dict) -> str:
    recording = side_car["recording"]
    if recording is None:
        return "none"
    return recording.get("digest_state", "calculated")


def make_tools(directory: Path, build: Path, real: bool) -> tuple[Path, dict]:
    directory.mkdir()
    for name in NATIVE:
        (directory / name).symlink_to(build / name)
    if real:
        return directory, {}
    from test_profile_cpu import SCRIPT, SRCLINES
    perf = directory / "perf"
    perf.write_text(f"#!/bin/sh\nexec {sys.executable} "
                    f"{ROOT / 'tests' / 'fake_perf.py'} \"$@\"\n")
    perf.chmod(0o755)
    script = directory / "script.txt"
    script.write_text("\n".join(line for line in SCRIPT.splitlines()
                                if "PERF_RECORD_LOST" not in line) + "\n")
    srclines = directory / "srclines.txt"
    srclines.write_text(SRCLINES)
    return directory, {"FAKE_PERF_SCRIPT": str(script),
                       "FAKE_PERF_SRCLINES": str(srclines)}


def write_manifest(kind: str, home: Path, build: Path, source: Path,
                   scale: float) -> Path:
    duration_s = max(0.1, 1000 * scale)
    if kind == "native":
        manifest = _multitask_manifest(duration_s, build)
    else:
        manifest = _toy_manifest(int(duration_s * 1000) * MS)
        manifest.add_process(
            "worker", command=[sys.executable,
                               str(source / "tests/participants/echo.py")],
            step_period_ns=10 * MS)
    return manifest.write(home / f"{kind}.json").path


def capture_once(case: str, manifest: Path, mode: str, build_source: Path,
                 tools: Path, fake: dict, work: Path) -> dict:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    policy = [] if mode == "default" else ["--provenance-mode", mode]
    result = work / "driver.json"
    system_path = os.environ["PATH"] if not fake else "/usr/bin:/bin"
    environment = {**os.environ, **fake,
                   "PATH": os.pathsep.join([str(tools), system_path]),
                   "PYTHONPATH": str(build_source / "python" / "src")}
    begin = time.perf_counter_ns()
    done = subprocess.run(
        [sys.executable, "-c", DRIVER, str(result), str(manifest),
         "--out", "prof", *CASES[case][1], *policy],
        cwd=work, env=environment, capture_output=True, text=True)
    command_ns = time.perf_counter_ns() - begin
    if done.returncode != 0:
        raise SystemExit(f"{case} {mode}: exit {done.returncode}\n{done.stderr}")
    driver = json.loads(result.read_text())
    out = (work / "prof").resolve()
    # A capture can be incomplete while its Run succeeded, for example with an
    # Activation join that a host cannot prove. The Run is what must succeed.
    status = json.loads((out / "profile.json").read_text())["run"]["status"]
    if status != "succeeded":
        raise SystemExit(f"{case} {mode}: Run {status}\n{done.stderr}")
    side_car_path = out / "run.mcap.provenance.json"
    side_car = json.loads(side_car_path.read_text())
    rows = [{**row, "path": str(Path(row["path"]).relative_to(out))}
            for row in driver["hashes"]]
    verify_profile_hashes(rows, out)
    report = json.loads((out / "run.diagnostics.json").read_text())
    recording = out / "run.mcap"
    with open(recording, "rb") as file:
        recording_digest = hashlib.file_digest(file, "sha256").hexdigest()
    return {
        "exit_code": driver["exit_code"], "command_process_ns": command_ns,
        "phases": split_phases(
            driver["start_ns"], side_car_path.stat().st_mtime_ns,
            driver["end_ns"], sum(row["ns"] for row in rows)),
        "run_wall_ns": report["observational"]["run_wall_time"].get("wall_ns"),
        "runner": {"recording_state": recording_state(side_car),
                   "input_digests": input_digests(side_car)},
        "profile_hashes": rows,
        "recording_bytes": recording.stat().st_size,
        "recording_sha256": recording_digest,
    }


def summarize(runs: list[dict]) -> dict:
    def spread(values: list[int]) -> dict:
        return {"min": min(values), "median": statistics.median(values),
                "max": max(values)}

    last = runs[-1]
    return {
        **{name: spread([run["phases"][name] for run in runs])
           for name in runs[0]["phases"]},
        "profile_hash_operations": len(last["profile_hashes"]),
        "profile_hash_bytes": sum(row["bytes"] for row in last["profile_hashes"]),
        "runner_input_digests": last["runner"]["input_digests"],
        "runner_recording_state": last["runner"]["recording_state"],
        "recording_bytes": last["recording_bytes"],
    }


def markdown(document: dict) -> str:
    lines = [f"# Profile command cost: {document['label']}", "",
             f"- Machine: {document['host']['machine']}",
             f"- Revision: `{document['build']['source_revision']}`",
             f"- Tools: {document['conditions']['tools']}. "
             f"{document['conditions']['tools_statement']}",
             f"- Load: {document['conditions']['load']}",
             f"- Repetitions: {document['conditions']['repetitions']}",
             f"- Runner end: {document['conditions']['runner_end']}", "",
             "Values in ms: min / median / max.", "",
             "| Case | Mode | End to end | Capture and Run | Post-Run | "
             "Profile hash | Profile hash ops | Profile hash MiB | "
             "Runner input digests | Runner Recording |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]

    def triple(spread: dict) -> str:
        return " / ".join(f"{spread[key] / MS:.1f}"
                          for key in ("min", "median", "max"))

    for case, modes in document["cases"].items():
        for mode, values in modes.items():
            summary = values["summary"]
            lines.append(
                f"| {case} | {mode} | {triple(summary['end_to_end_ns'])} | "
                f"{triple(summary['capture_and_run_ns'])} | "
                f"{triple(summary['post_run_ns'])} | "
                f"{triple(summary['profile_hash_ns'])} | "
                f"{summary['profile_hash_operations']} | "
                f"{summary['profile_hash_bytes'] / (1 << 20):.1f} | "
                f"{summary['runner_input_digests']['entries']} | "
                f"{summary['runner_recording_state']} |")
    return "\n".join(lines) + "\n"


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--revision", help="build this commit in isolation")
    choice.add_argument("--build-dir", type=Path,
                        help="use this existing Release build")
    parser.add_argument("--machine", required=True)
    parser.add_argument("--load-condition", required=True)
    parser.add_argument("--repetitions", type=int, default=9)
    parser.add_argument("--scale", type=float, default=1.0,
                        help="multiply the Recording Duration")
    parser.add_argument("--case", action="append", choices=list(CASES),
                        help="measure this case; can repeat; default cpu "
                             "and cpu-activations")
    parser.add_argument("--mode", action="append",
                        choices=["default", "full", "external"],
                        help="measure this policy; can repeat; default full "
                             "and external. A revision before the option "
                             "needs `default`.")
    parser.add_argument("--real-tools", action="store_true",
                        help="use perf or Heaptrack from PATH")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / "sil-hash-builds")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "bench")
    parser.add_argument("--label", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    build = (build_revision(args.revision, args.cache_dir)
             if args.revision else args.build_dir.resolve())
    identity = select_build(build)
    source = Path(_cache_entries(build)["CMAKE_HOME_DIRECTORY"])
    cases = args.case or ["cpu", "cpu-activations"]
    modes = args.mode or ["full", "external"]
    document = {
        "sil_profile_cost": 1, "label": args.label,
        "host": host_identity(args.machine), "build": identity,
        "conditions": {
            "load": args.load_condition, "repetitions": args.repetitions,
            "scale": args.scale, "runner_end": RUNNER_END,
            "tools": "real collector" if args.real_tools else "fake perf",
            "tools_statement": REAL_STATEMENT if args.real_tools
            else FAKE_STATEMENT,
            "cache": "Warm: one discarded command precedes each series."},
        "cases": {}}
    with tempfile.TemporaryDirectory(prefix="sil-profile-cost-") as scratch:
        home = Path(scratch).resolve()
        tools, fake = make_tools(home / "tools", build, args.real_tools)
        for case in cases:
            manifest = write_manifest(CASES[case][0], home, build, source,
                                      args.scale)
            document["cases"][case] = {}
            for mode in modes:
                work = home / "work"
                capture_once(case, manifest, mode, source, tools, fake, work)
                runs = [capture_once(case, manifest, mode, source, tools, fake,
                                     work)
                        for _ in range(args.repetitions)]
                document["cases"][case][mode] = {"summary": summarize(runs),
                                                 "runs": runs}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"profile-cost-{args.label}.json").write_text(
        json.dumps(document, indent=2) + "\n")
    (args.out_dir / f"profile-cost-{args.label}-results.md").write_text(
        markdown(document))
    return 0


if __name__ == "__main__":
    sys.exit(main())
