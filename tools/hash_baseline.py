#!/usr/bin/env python3
"""Hashing cost baseline of a selected SiL build or revision (#324).

Each repetition of each workload runs two series. The ordinary series is
`sil-run` with no observer: its end-to-end time is the cost of an ordinary Run.
The attributed series is `sil-run-diagnostics` under tools/hash_trace.c: the
diagnostics report gives the phases, and the trace gives the hash work per role.
Both series must write the same Recording bytes, and each digest in a side-car
must match an independent SHA-256, or the tool stops.

    tools/hash_baseline.py --revision <commit> --machine "<name>" \\
        --load-condition "<what else runs>"

Writes `hash-baseline-<label>.json` (raw) and `hash-baseline-<label>-results.md`
into docs/bench/; `hash-baseline.md` there explains them. Timings describe one
machine and build. No test or gate reads them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from sil.manifest import Manifest  # noqa: E402

MS = 1_000_000
MIB = 1 << 20
HASH_ROLES = ("manifest", "provenance_inputs", "replay_verification", "output")
COLD_CACHE_STATEMENT = (
    "Cold: the tool cannot control this. Dropping the page cache needs "
    "administrator rights, so this capture has no cold-cache series."
)


class EvidenceError(Exception):
    """A capture contradicts itself, so it must not become evidence."""


def _complete(span: dict) -> bool:
    return span["offset"] == 0 and span["size"] > 0 \
        and span["bytes"] == span["size"]


def attribute_hash_work(trace: dict, roles: dict[str, str]) -> dict:
    """Turn a read trace into hash work per role.

    A hash operation is one complete sequential read of a file, from offset 0
    to its size at open. `roles` maps a resolved path to its role. All other
    reads stay in `other_reads`. For Replay, the runner verifies first and then
    decodes, so only the first complete pass of a file is verification.
    """
    if trace["dropped_files"]:
        raise EvidenceError(
            f"trace dropped {trace['dropped_files']} files: table is full")
    work = {role: {"operations": 0, "bytes": 0, "time_ns": 0}
            for role in HASH_ROLES}
    work["other_reads"] = {"bytes": 0}
    for file in trace["files"]:
        if file["dropped_spans"]:
            raise EvidenceError(
                f"trace dropped {file['dropped_spans']} spans of "
                f"{file['path']}: table is full")
        role = roles.get(file["path"])
        verified = False
        attributed = 0
        for span in file["spans"]:
            if role is None or not _complete(span):
                continue
            if role == "replay_verification" and verified:
                continue
            verified = True
            row = work[role]
            row["operations"] += 1
            row["bytes"] += span["bytes"]
            row["time_ns"] += span["end_ns"] - span["start_ns"]
            attributed += span["bytes"]
        work["other_reads"]["bytes"] += file["bytes"] - attributed
    return work


class SelectionError(Exception):
    """The requested build or revision cannot supply a valid baseline."""


def _digest(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _claimed_inputs(node) -> list[dict]:
    """Each artifact entry of a side-car that names a path and a digest."""
    if isinstance(node, list):
        return [found for item in node for found in _claimed_inputs(item)]
    if not isinstance(node, dict):
        return []
    if isinstance(node.get("path"), str):
        return [node] if isinstance(node.get("sha256"), str) else []
    return [found for value in node.values() for found in _claimed_inputs(value)]


def verify_references(side_car: dict, recording: Path,
                      digests: dict[str, str]) -> dict:
    """Compare every digest the side-car claims with an independent SHA-256.

    An entry without a digest string makes no claim, so it passes. That keeps
    this check valid for a schema that states an intentional omission.
    """
    claimed = _claimed_inputs(side_car.get("artifacts", {}))
    for entry in claimed:
        actual = digests.setdefault(entry["path"], _digest(Path(entry["path"])))
        if actual != entry["sha256"]:
            raise EvidenceError(
                f"side-car digest of {entry['path']} is {entry['sha256']}, "
                f"independent SHA-256 is {actual}")
    output = side_car.get("recording")
    digest = output.get("sha256") if isinstance(output, dict) else None
    if isinstance(digest, str):
        actual = _digest(recording)
        if actual != digest:
            raise EvidenceError(
                f"side-car digest of Recording {recording} is {digest}, "
                f"independent SHA-256 is {actual}")
    return {"inputs": len(claimed),
            "recording_digest_present": isinstance(digest, str)}


def inputs_without_observed_hash(side_car: dict, trace: dict) -> list[str]:
    """Inputs with a claimed digest but no complete read in the trace.

    A nonempty list means that the hash work of the Run is more than the trace
    shows, for example because a file was hashed through a memory map.
    """
    hashed = {file["path"] for file in trace["files"]
              if any(_complete(span) for span in file["spans"])}
    return sorted({str(Path(entry["path"]).resolve())
                   for entry in _claimed_inputs(side_car.get("artifacts", {}))}
                  - hashed)


# Runner selection -------------------------------------------------------------

def _git(source: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=source, capture_output=True,
                          text=True, check=True).stdout.strip()


def build_revision(revision: str, cache_dir: Path, repository: Path = ROOT,
                   ) -> Path:
    """Build a commit in its own clone and return the build directory.

    The clone keeps the checkout, its branches and its worktree list untouched
    and ignores uncommitted edits. A completed build of the commit is reused.
    """
    try:
        commit = _git(repository, "rev-parse", "--verify",
                      f"{revision}^{{commit}}")
    except subprocess.CalledProcessError as error:
        raise SelectionError(f"unknown revision {revision}") from error
    home = cache_dir.resolve() / commit
    build = home / "build"
    if (home / "complete").exists():
        return build
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    source = home / "source"
    steps = [
        ["git", "clone", "--quiet", "--shared", "--no-checkout",
         str(repository), str(source)],
        ["git", "-C", str(source), "checkout", "--quiet", "--detach", commit],
        ["cmake", "-S", str(source), "-B", str(build),
         "-DCMAKE_BUILD_TYPE=Release", f"-DSIL_SOURCE_REVISION={commit}"],
        ["cmake", "--build", str(build), "-j"],
    ]
    for step in steps:
        result = subprocess.run(step, capture_output=True, text=True)
        if result.returncode != 0:
            raise SelectionError(
                f"cannot build revision {commit}: {' '.join(step)}\n"
                f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    (home / "complete").write_text(commit + "\n")
    return build


def _cache_entries(build: Path) -> dict[str, str]:
    cache = build / "CMakeCache.txt"
    if not cache.exists():
        raise SelectionError(f"{build} has no CMakeCache.txt")
    entries = {}
    for line in cache.read_text().splitlines():
        if ":" in line and "=" in line and not line.startswith(("#", "//")):
            name, value = line.split("=", 1)
            entries[name.split(":", 1)[0]] = value
    return entries


def select_build(build: Path) -> dict:
    """Identify a build directory. Only a Release build is baseline evidence."""
    entries = _cache_entries(build)
    build_type = entries.get("CMAKE_BUILD_TYPE")
    if build_type != "Release":
        raise SelectionError(
            f"{build} is a {build_type or 'untyped'} build, not Release")
    source = Path(entries["CMAKE_HOME_DIRECTORY"])
    for runner in ("sil-run", "sil-run-diagnostics"):
        if not (build / runner).exists():
            raise SelectionError(f"{build} has no {runner}")
    version = subprocess.run([str(build / "sil-run"), "--version"],
                             capture_output=True, text=True).stdout.strip()
    return {"source_revision": _git(source, "rev-parse", "HEAD"),
            "worktree_clean": _git(source, "status", "--porcelain") == "",
            "cmake_build_type": build_type,
            "cxx_compiler": entries.get("CMAKE_CXX_COMPILER"),
            "sil_version": version}


def compile_tracer(directory: Path) -> Path:
    library = directory / "hash_trace.so"
    command = ["cc", "-shared", "-fPIC", "-O2", "-o", str(library),
               str(ROOT / "tools" / "hash_trace.c")]
    if sys.platform != "darwin":
        command += ["-ldl", "-pthread"]
    subprocess.run(command, check=True)
    return library


# Workloads --------------------------------------------------------------------

@dataclass
class Workload:
    description: str
    manifest: Path
    participants: int
    artifacts: list[dict] = field(default_factory=list)
    stimulus: Path | None = None


def _pad(source: Path, target: Path, size: int, seed: int) -> Path:
    """Copy a library and append bytes up to `size`, unique for each seed.

    The loader ignores bytes past the end of the image, so the library stays
    valid and its size is under the control of the workload.
    """
    block = hashlib.sha256(str(seed).encode()).digest() * (MIB // 32)
    with open(target, "wb") as out:
        out.write(source.read_bytes())
        remaining = size - out.tell()
        while remaining > 0:
            out.write(block[:remaining])
            remaining -= len(block)
    target.chmod(0o755)
    return target


def _toy_manifest(duration_ns: int) -> Manifest:
    schemas = json.loads(
        (ROOT / "tests/fixtures/schemas/toy.json").read_text())
    manifest = Manifest(duration_ns=duration_ns, epoch_ns=0)
    manifest.add_schemas(schemas)
    return manifest


def _add_producer(manifest: Manifest, name: str, library: Path) -> None:
    manifest.add_channel(name, schema="toy.Counter")
    manifest.add_native(
        name, library=str(library),
        config={"channel": name, "period_ns": 10 * MS}, publishes=[name])


def _size(mib: int, scale: float) -> int:
    return max(1, int(mib * MIB * scale))


def native_shared_library(home: Path, build: Path, scale: float) -> Workload:
    library = _pad(build / "toy_producer.silp", home / "shared.silp",
                   _size(8, scale), 1)
    manifest = _toy_manifest(100 * MS)
    for index in range(32):
        _add_producer(manifest, f"p{index:02}", library)
    return Workload(
        "32 Native Participants share one library, 100 ms Duration, "
        "10 ms Period",
        manifest.write(home / "manifest.json").path, 32,
        [{"kind": "library", "bytes": library.stat().st_size}])


def native_distinct_artifacts(home: Path, build: Path, scale: float) -> Workload:
    manifest = _toy_manifest(100 * MS)
    artifacts = []
    for index in range(4):
        library = _pad(build / "toy_producer.silp", home / f"p{index}.silp",
                       _size(128, scale), index + 10)
        _add_producer(manifest, f"p{index}", library)
        artifacts.append({"kind": "library", "bytes": library.stat().st_size})
    return Workload(
        "4 Native Participants, each with its own large library, 100 ms "
        "Duration",
        manifest.write(home / "manifest.json").path, 4, artifacts)


def _multitask_manifest(duration_s: float, build: Path) -> Manifest:
    manifest = _toy_manifest(int(duration_s * 1000) * MS)
    manifest.add_channel("ticks", schema="toy.Counter")
    manifest.add_native(
        "multi", library=str(build / "toy_multitask.silp"),
        config={"channel": "ticks", "fast_period_ns": 1 * MS,
                "slow_period_ns": 5 * MS, "seed": 7},
        publishes=["ticks"])
    return manifest


def native_recording_heavy(home: Path, build: Path, scale: float) -> Workload:
    duration_s = max(0.1, 1000 * scale)
    return Workload(
        f"1 Native Participant, 2 Tasks (1 ms, 5 ms), {duration_s:g} s "
        f"Duration: {int(duration_s * 1200)} Messages",
        _multitask_manifest(duration_s, build).write(
            home / "manifest.json").path, 1,
        [{"kind": "library",
          "bytes": (build / "toy_multitask.silp").stat().st_size}])


def process_shared_inputs(home: Path, build: Path, scale: float) -> Workload:
    source = Path(_cache_entries(build)["CMAKE_HOME_DIRECTORY"])
    script = source / "tests" / "participants" / "echo.py"
    data = home / "shared-input.bin"
    size = _size(32, scale)
    data.write_bytes((bytes(range(256)) * (size // 256 + 1))[:size])
    manifest = _toy_manifest(100 * MS)
    for index in range(8):
        name = f"p{index}"
        manifest.add_channel(f"{name}-out", schema="toy.Counter")
        manifest.add_process(
            name, command=[sys.executable, str(script), str(data)],
            step_period_ns=10 * MS, publishes=[f"{name}-out"])
    interpreter = Path(sys.executable).resolve()
    return Workload(
        "8 Python Process participants share an interpreter, a script and "
        "one file argument, 100 ms Duration, 10 ms Step",
        manifest.write(home / "manifest.json").path, 8,
        [{"kind": "file_argument", "bytes": size},
         {"kind": "script", "bytes": script.stat().st_size},
         {"kind": "interpreter", "bytes": interpreter.stat().st_size}])


def replay_verification(home: Path, build: Path, scale: float) -> Workload:
    duration_s = max(0.1, 100 * scale)
    source = _multitask_manifest(duration_s, build).write(
        home / "source.json").path
    stimulus = home / "stimulus.mcap"
    subprocess.run([str(build / "sil-run"), str(source), "-o", str(stimulus)],
                   check=True, cwd=home, capture_output=True)
    manifest = _toy_manifest(int(duration_s * 1000) * MS)
    manifest.add_channel("ticks", schema="toy.Counter")
    manifest.add_replay("replay", recording=str(stimulus), channels=["ticks"])
    return Workload(
        f"Replay of a {duration_s:g} s Recording of the multi-Task "
        "Participant, with its digest committed in the Manifest",
        manifest.write(home / "manifest.json").path, 1,
        [{"kind": "replay_recording", "bytes": stimulus.stat().st_size}],
        stimulus)


WORKLOADS = {
    "native-shared-library": native_shared_library,
    "native-distinct-artifacts": native_distinct_artifacts,
    "native-recording-heavy": native_recording_heavy,
    "process-shared-inputs": process_shared_inputs,
    "replay-verification": replay_verification,
}


# One Run ----------------------------------------------------------------------

def _run_environment(build: Path) -> dict[str, str]:
    source = _cache_entries(build)["CMAKE_HOME_DIRECTORY"]
    return {**os.environ, "PYTHONPATH": str(Path(source) / "python" / "src")}


def _timed_run(command: list[str], work: Path, environment: dict) -> int:
    started = time.monotonic_ns()
    result = subprocess.run(command, cwd=work, env=environment,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    elapsed = time.monotonic_ns() - started
    if result.returncode != 0:
        raise EvidenceError(
            f"{Path(command[0]).name} exit code {result.returncode}: "
            f"{result.stderr.decode(errors='replace')[-2000:]}")
    return elapsed


def _clean(work: Path) -> None:
    for path in work.glob("run.*"):
        path.unlink()


def run_ordinary(build: Path, workload: Workload, work: Path,
                 runner_args: list[str]) -> dict:
    """`sil-run` with no observer: the end-to-end cost of an ordinary Run."""
    recording = work / "run.mcap"
    elapsed = _timed_run(
        [str(build / "sil-run"), str(workload.manifest), "-o", str(recording),
         *runner_args], work, _run_environment(build))
    digest = _digest(recording)
    _clean(work)
    return {"series": "ordinary", "end_to_end_ns": elapsed,
            "recording_sha256": digest}


def _phases(report: dict, end_to_end_ns: int, output_hash_ns: int) -> dict:
    observational = report["observational"]
    phases = observational["phases"]
    close = phases["recording_finalization_ns"]
    run_wall = observational["run_wall_time"]["wall_ns"]
    return {
        "preparation_ns": phases["preflight_ns"],
        "execution_recording_ns": phases["setup_ns"]
        + phases["scheduled_execution_ns"] + phases["shutdown_ns"],
        "recording_close_ns": close,
        "output_hash_ns": output_hash_ns,
        "finalization_ns": close + output_hash_ns,
        "reported_run_wall_ns": run_wall,
        "outside_reported_run_wall_ns": end_to_end_ns - run_wall,
    }


def _hash_roles(workload: Workload, recording: Path,
                side_car: dict) -> dict[str, str]:
    roles = {str(Path(entry["path"]).resolve()): "provenance_inputs"
             for entry in _claimed_inputs(side_car.get("artifacts", {}))}
    roles[str(workload.manifest.resolve())] = "manifest"
    roles[str(recording.resolve())] = "output"
    if workload.stimulus:
        roles[str(workload.stimulus.resolve())] = "replay_verification"
    return roles


def run_attributed(build: Path, workload: Workload, work: Path, tracer: Path,
                   runner_args: list[str], digests: dict[str, str]) -> dict:
    """The diagnostics runner under the tracer: phases and hash work."""
    recording = work / "run.mcap"
    report_path = work / "run.report.json"
    trace_path = work / "run.trace.json"
    preload = ("DYLD_INSERT_LIBRARIES" if sys.platform == "darwin"
               else "LD_PRELOAD")
    elapsed = _timed_run(
        [str(build / "sil-run-diagnostics"), str(workload.manifest), "-o",
         str(recording), "--diagnostics-out", str(report_path), *runner_args],
        work, {**_run_environment(build), preload: str(tracer),
               "SIL_HASH_TRACE_OUT": str(trace_path)})
    side_car = json.loads(Path(f"{recording}.provenance.json").read_text())
    references = verify_references(side_car, recording, digests)
    trace = json.loads(trace_path.read_text())
    hash_work = attribute_hash_work(
        trace, _hash_roles(workload, recording, side_car))
    result = {
        "series": "attributed", "end_to_end_ns": elapsed,
        "recording_sha256": _digest(recording),
        "recording_bytes": recording.stat().st_size,
        "runner_bytes": (build / "sil-run-diagnostics").stat().st_size,
        "phases": _phases(json.loads(report_path.read_text()), elapsed,
                          hash_work["output"]["time_ns"]),
        "hash_work": hash_work, "references": references,
        "output_digest_without_reread":
            references["recording_digest_present"]
            and hash_work["output"]["operations"] == 0,
        "inputs_without_observed_hash":
            inputs_without_observed_hash(side_car, trace),
        "trace_read_calls": sum(f["reads"] for f in trace["files"]),
    }
    _clean(work)
    return result


# Capture ----------------------------------------------------------------------

def _spread(values: list[int]) -> dict:
    return {"min": min(values), "median": statistics.median(values),
            "max": max(values), "count": len(values)}


def summarize(runs: list[dict]) -> dict:
    attributed = [r for r in runs if r["series"] == "attributed"]
    ordinary = [r for r in runs if r["series"] == "ordinary"]
    return {
        "recordings_identical":
            len({r["recording_sha256"] for r in runs}) == 1,
        "end_to_end_ns": {
            "ordinary": _spread([r["end_to_end_ns"] for r in ordinary]),
            "attributed": _spread([r["end_to_end_ns"] for r in attributed]),
        },
        "phases": {name: _spread([r["phases"][name] for r in attributed])
                   for name in attributed[0]["phases"]},
        "hash_work": {
            role: {key: _spread([r["hash_work"][role][key] for r in attributed])
                   for key in ("operations", "bytes", "time_ns")}
            for role in HASH_ROLES},
        "other_read_bytes": _spread(
            [r["hash_work"]["other_reads"]["bytes"] for r in attributed]),
        "output_digest_without_reread": any(
            r["output_digest_without_reread"] for r in attributed),
        "inputs_without_observed_hash": sorted({
            path for r in attributed
            for path in r["inputs_without_observed_hash"]}),
    }


def capture_workload(name: str, build: Path, scratch: Path, tracer: Path,
                     repetitions: int, scale: float,
                     runner_args: list[str]) -> dict:
    home = scratch / name
    work = home / "work"
    work.mkdir(parents=True)
    workload = WORKLOADS[name](home, build, scale)
    digests: dict[str, str] = {}
    # A discarded Run: each input was just written or is now read, so each
    # measured Run starts with a warm page cache.
    run_ordinary(build, workload, work, runner_args)
    load_before = os.getloadavg()[0]
    runs = []
    for repetition in range(1, repetitions + 1):
        runs.append({"repetition": repetition,
                     **run_ordinary(build, workload, work, runner_args)})
        runs.append({"repetition": repetition,
                     **run_attributed(build, workload, work, tracer,
                                      runner_args, digests)})
    summary = summarize(runs)
    if not summary["recordings_identical"]:
        raise EvidenceError(f"{name}: Runs wrote different Recordings")
    document = {
        "description": workload.description,
        "participants": workload.participants,
        "artifacts": workload.artifacts,
        "manifest_bytes": workload.manifest.stat().st_size,
        "recording_bytes": next(r["recording_bytes"] for r in runs
                                if r["series"] == "attributed"),
        "load_average_1m": {"before": load_before,
                            "after": os.getloadavg()[0]},
        "summary": summary,
        "runs": runs,
    }
    shutil.rmtree(home)
    return document


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
            "architecture": platform.machine(), "cpu": cpu,
            "logical_cpus": os.cpu_count()}


# Report -----------------------------------------------------------------------

def _triple(spread: dict) -> str:
    return (f"{spread['min'] / MS:.1f} / {spread['median'] / MS:.1f} / "
            f"{spread['max'] / MS:.1f}")


def markdown(document: dict) -> str:
    host, build, conditions = (document["host"], document["build"],
                               document["conditions"])
    lines = [
        "# Hashing cost baseline (generated)",
        "",
        "Generated by `tools/hash_baseline.py`; "
        "[hash-baseline.md](hash-baseline.md) explains it. Times are "
        "min / median / max in ms.",
        "",
        f"- Machine: {host['machine']}; {host['cpu']}; "
        f"{host['logical_cpus']} logical CPUs; {host['os']} "
        f"({host['architecture']})",
        f"- Build: SiL {build['sil_version']}, revision "
        f"`{build['source_revision']}`"
        f"{'' if build['worktree_clean'] else ' plus uncommitted changes'}, "
        f"CMake `{build['cmake_build_type']}`",
        f"- Backend: {conditions['backend']}",
        f"- Load: {conditions['load']}",
        f"- Repetitions per workload: {conditions['repetitions']}; "
        f"scale {conditions['scale']:g}; runner arguments: "
        f"{' '.join(conditions['runner_args']) or 'none'}",
        f"- Cache: {conditions['warm_cache']} {conditions['cold_cache']}",
        "- Timing boundary: the reported Run wall time excludes the hash of "
        "the Recording after its close. The end-to-end time includes it.",
        "",
        "| Workload | Inputs (MiB) | Recording (MiB) | Ordinary end to end | "
        "Attributed end to end | Preparation | Execution and Recording | "
        "Finalization | Reported Run wall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, workload in document["workloads"].items():
        summary, phases = workload["summary"], workload["summary"]["phases"]
        inputs = sum(a["bytes"] for a in workload["artifacts"]) / MIB
        lines.append(
            f"| {name} | {inputs:.1f} | {workload['recording_bytes'] / MIB:.1f}"
            f" | {_triple(summary['end_to_end_ns']['ordinary'])}"
            f" | {_triple(summary['end_to_end_ns']['attributed'])}"
            f" | {_triple(phases['preparation_ns'])}"
            f" | {_triple(phases['execution_recording_ns'])}"
            f" | {_triple(phases['finalization_ns'])}"
            f" | {_triple(phases['reported_run_wall_ns'])} |")
    lines += ["", "Hash work of the attributed series:", "",
              "| Workload | Role | Operations (median) | MiB (median) | "
              "Time |",
              "| --- | --- | --- | --- | --- |"]
    for name, workload in document["workloads"].items():
        for role in HASH_ROLES:
            row = workload["summary"]["hash_work"][role]
            lines.append(
                f"| {name} | {role} | {row['operations']['median']:g} | "
                f"{row['bytes']['median'] / MIB:.2f} | "
                f"{_triple(row['time_ns'])} |")
    return "\n".join(lines) + "\n"


# Command line -----------------------------------------------------------------

def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--revision", help="build this commit in isolation")
    selected.add_argument("--build-dir", type=Path,
                          help="use this existing Release build")
    parser.add_argument("--machine", required=True,
                        help="a name for the machine, kept in the evidence")
    parser.add_argument("--load-condition", required=True,
                        help="what else the machine does, kept in the evidence")
    parser.add_argument("--backend", default="the SHA-256 backend of the build",
                        help="SHA-256 backend statement, kept in the evidence")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--scale", type=float, default=1.0,
                        help="multiply artifact sizes and Recording Durations")
    parser.add_argument("--workload", action="append", choices=list(WORKLOADS),
                        help="measure only this workload; can repeat")
    parser.add_argument("--runner-arg", action="append", default=[],
                        help="pass this argument to both runners; can repeat")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / "sil-hash-builds",
                        help="where --revision keeps its isolated builds")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "bench")
    parser.add_argument("--label", help="name of the evidence files; the "
                        "default is the first 12 characters of the revision")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    build = (build_revision(args.revision, args.cache_dir)
             if args.revision else args.build_dir.resolve())
    identity = select_build(build)
    conditions = {
        "load": args.load_condition, "repetitions": args.repetitions,
        "scale": args.scale, "backend": args.backend,
        "runner_args": args.runner_arg,
        "warm_cache": "Warm: one discarded Run precedes the series of each "
                      "workload.",
        "cold_cache": COLD_CACHE_STATEMENT,
    }
    document = {"sil_hash_baseline": 1, "host": host_identity(args.machine),
                "build": identity, "conditions": conditions, "workloads": {}}
    with tempfile.TemporaryDirectory(prefix="sil-hash-") as scratch:
        scratch = Path(scratch).resolve()
        tracer = compile_tracer(scratch)
        for name in args.workload or WORKLOADS:
            document["workloads"][name] = capture_workload(
                name, build, scratch, tracer, args.repetitions, args.scale,
                args.runner_arg)
            median = document["workloads"][name]["summary"]["end_to_end_ns"][
                "ordinary"]["median"]
            print(f"{name}: ordinary median {median / MS:.1f} ms",
                  file=sys.stderr)
    label = args.label or identity["source_revision"][:12]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"hash-baseline-{label}.json").write_text(
        json.dumps(document, indent=2) + "\n")
    (args.out_dir / f"hash-baseline-{label}-results.md").write_text(
        markdown(document))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, SelectionError) as error:
        raise SystemExit(f"hash_baseline: {error}") from None
