"""The perf and Heaptrack feasibility proof (issue #314).

    python proof.py probe OUT    # capability probes only: OUT/probe.json
    python proof.py run OUT      # the full proof: OUT/results.json, .md

`probe` records the host facts (kernel, architecture, perf_event_paranoid,
capabilities, seccomp, PID namespace) and tries each collector once on
/bin/true. `run` also builds the workload, makes the Runs below with the
same Manifest (the Heaptrack Process scope wraps two commands) and checks
the evidence:

| Run | Collector |
| --- | --- |
| ordinary | none |
| perf-dwarf | perf record, cpu-clock:u, DWARF call frames |
| perf-fp | perf record, cpu-clock:u, frame pointers |
| heaptrack-runner | Heaptrack preloaded into sil-run, matching debug file |
| heaptrack-runner-mismatched | the same, with a mismatched debug file |
| heaptrack-fmu | Heaptrack in the unshimmed FMU participant (live) |
| heaptrack-shimmed-process | Heaptrack in the shimmed Process participant |
| heaptrack-shimmed-process-sleep-immediate | the same, shim sleep |
|   | "immediate"; the FMU participant raw, interpreted after the Run |

It needs on PATH: sil-run, perf, objcopy, readelf, cc, and this Python with
the sil package. Heaptrack's preload library and interpreter are found under
/usr/lib or named by SILPROOF_HEAPTRACK_PRELOAD and
SILPROOF_HEAPTRACK_INTERPRET. A check failure exits 1; a finding is
recorded and does not fail. A backend that its probe finds unavailable is a
failed check: its Runs are skipped and nothing about it is qualified. No
check uses a timing, a sample percentage or an overhead threshold.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROOF_DIR))
import analyze  # noqa: E402
import workload  # noqa: E402

from sil.recording import read_records  # noqa: E402

LAUNCH = PROOF_DIR / "heaptrack-launch.sh"
PERF_EVENT = "cpu-clock:u"
PERF_FREQUENCY = "999"
PERF_CLOCK = "monotonic"
DWARF_STACK = "8192"
DEADLINE_MS = "60000"
COLLECTOR_WAIT_S = 120


# --- host facts -------------------------------------------------------------


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _status_fields(*names: str) -> dict[str, str | None]:
    status = _read("/proc/self/status") or ""
    found = dict(line.split(":", 1) for line in status.splitlines()
                 if ":" in line)
    return {n: found[n].strip() if n in found else None for n in names}


def _version(command: list[str]) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (done.stdout or done.stderr).strip()
    return text.splitlines()[0] if text else None


def heaptrack_paths() -> tuple[Path | None, Path | None]:
    preload = os.environ.get("SILPROOF_HEAPTRACK_PRELOAD")
    interpret = os.environ.get("SILPROOF_HEAPTRACK_INTERPRET")
    if not preload:
        found = sorted(Path("/usr/lib").glob(
            "**/heaptrack/libheaptrack_preload.so"))
        preload = str(found[0]) if found else None
    if not interpret:
        found = sorted(Path("/usr/lib").glob(
            "**/heaptrack/libexec/heaptrack_interpret"))
        interpret = str(found[0]) if found else None
    return (Path(preload) if preload else None,
            Path(interpret) if interpret else None)


def _packages() -> list[str]:
    try:
        done = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Package}=${Version}\\n",
             "linux-perf*", "linux-tools-*", "heaptrack", "gcc", "binutils"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted(line for line in done.stdout.splitlines()
                  if line and not line.endswith("="))


def host_facts() -> dict:
    preload, interpret = heaptrack_paths()
    return {
        "packages": _packages(),
        "uname": platform.uname()._asdict(),
        "libc": " ".join(platform.libc_ver()),
        "python": platform.python_version(),
        "in_container": Path("/.dockerenv").exists(),
        "uid": os.getuid(),
        "perf_event_paranoid": _read("/proc/sys/kernel/perf_event_paranoid"),
        "perf_event_max_sample_rate":
            _read("/proc/sys/kernel/perf_event_max_sample_rate"),
        "kptr_restrict": _read("/proc/sys/kernel/kptr_restrict"),
        "status": _status_fields("CapEff", "CapPrm", "Seccomp", "NSpid"),
        "tools": {
            "perf": _version(["perf", "--version"]),
            "heaptrack": _version(["heaptrack", "--version"]),
            "heaptrack_preload": str(preload) if preload else None,
            "heaptrack_interpret": str(interpret) if interpret else None,
            "cc": _version(["cc", "--version"]),
            "objcopy": _version(["objcopy", "--version"]),
            "sil-run": _version(["sil-run", "--version"]),
        },
    }


# --- probes -----------------------------------------------------------------


def _probe_perf(out: Path, name: str, extra: list[str]) -> dict:
    data = out / f"{name}.data"
    command = ["perf", "record", "-e", PERF_EVENT, "-F", "99", "-k",
               PERF_CLOCK, *extra, "-o", str(data), "--", "true"]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=120)
        returncode, stderr = done.returncode, done.stderr
    except OSError as error:
        returncode, stderr = None, str(error)
    produced = data.exists() and data.stat().st_size > 0
    return {"command": command, "returncode": returncode,
            "stderr": stderr[-2000:],
            "outcome": analyze.probe_outcome(returncode, stderr,
                                             produced=produced)}


def _probe_heaptrack(out: Path) -> dict:
    preload, interpret = heaptrack_paths()
    if not preload or not interpret:
        return {"outcome": "missing", "preload": str(preload),
                "interpret": str(interpret)}
    raw = out / "heaptrack-probe.raw"
    env = {**os.environ, "LD_PRELOAD": str(preload),
           "DUMP_HEAPTRACK_OUTPUT": str(raw)}
    done = subprocess.run(["true"], env=env, capture_output=True, text=True)
    produced = raw.exists() and raw.stat().st_size > 0
    return {"returncode": done.returncode, "stderr": done.stderr[-2000:],
            "outcome": analyze.probe_outcome(done.returncode, done.stderr,
                                             produced=produced)}


def probe(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "host": host_facts(),
        "probes": {
            "perf_user": _probe_perf(out, "perf-user", []),
            "perf_user_dwarf": _probe_perf(
                out, "perf-dwarf", ["--call-graph", f"dwarf,{DWARF_STACK}"]),
            "perf_kernel": _probe_perf_kernel(out),
            "heaptrack_preload": _probe_heaptrack(out),
        },
    }
    for data in out.glob("*.data"):
        data.unlink()
    for raw in out.glob("*.raw"):
        raw.unlink()
    (out / "probe.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def _probe_perf_kernel(out: Path) -> dict:
    """Kernel-mode sampling: informational. The proof samples user space
    only, so a refusal here does not block it."""
    data = out / "perf-kernel.data"
    command = ["perf", "record", "-e", "cpu-clock", "-F", "99", "-o",
               str(data), "--", "true"]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=120)
        returncode, stderr = done.returncode, done.stderr
    except OSError as error:
        returncode, stderr = None, str(error)
    produced = data.exists() and data.stat().st_size > 0
    return {"command": command, "returncode": returncode,
            "stderr": stderr[-2000:],
            "outcome": analyze.probe_outcome(returncode, stderr,
                                             produced=produced)}


# --- artifacts --------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_id(path: Path) -> str | None:
    done = subprocess.run(["readelf", "-n", str(path)], capture_output=True,
                          text=True)
    return analyze.readelf_build_id(done.stdout)


def artifact_identities(a: workload.Artifacts) -> dict:
    files = {
        "native (stripped, loaded)": a.native,
        "native (before strip)": a.native_full,
        "native matching debug": a.native_debug,
        "native mismatched debug": a.native_mismatched_debug,
        "process library": a.process,
        "child": a.child,
        "fmu archive": a.fmu,
        "fmu library": a.fmu_binary,
    }
    return {name: {"path": str(p), "sha256": _sha256(p),
                   "bytes": p.stat().st_size,
                   "build_id": None if p.suffix == ".fmu" else build_id(p)}
            for name, p in files.items() if p is not None}


# --- Runs -------------------------------------------------------------------


def _clean_env(**extra: str) -> dict:
    env = dict(os.environ)
    # perf and elfutils ask a debuginfod server when this is set. The proof
    # resolves only local files.
    env.pop("DEBUGINFOD_URLS", None)
    env.update(extra)
    return env


def _leftovers(markers: list[str]) -> list[str]:
    """Processes still alive whose command line names a marker."""
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        text = cmdline.decode(errors="replace")
        if any(m in text for m in markers):
            found.append(f"{proc.name}: {text.strip()}")
    return found


class Run:
    def __init__(self, root: Path, name: str):
        self.name = name
        self.dir = root / "runs" / name
        self.sidecar = self.dir / "sidecar"
        self.sidecar.mkdir(parents=True)
        self.recording = self.dir / "run.mcap"
        self.result: dict = {"name": name}

    def execute(self, command: list[str], env: dict) -> int:
        self.result["command"] = command
        started = time.monotonic_ns()
        with open(self.dir / "stderr.txt", "wb") as stderr:
            done = subprocess.run(command, env=env, stdout=stderr,
                                  stderr=stderr, cwd=self.dir)
        self.result["wall_ns"] = time.monotonic_ns() - started
        self.result["returncode"] = done.returncode
        self.result["recording_bytes"] = (self.recording.stat().st_size
                                          if self.recording.exists() else None)
        return done.returncode

    def intervals(self) -> list[analyze.Interval]:
        return [i for f in sorted(self.sidecar.iterdir())
                for i in analyze.parse_sidecar(f.read_text())]


def sil_run_command(manifest: Path, run: Run) -> list[str]:
    return ["sil-run", str(manifest), "-o", str(run.recording),
            "--participant-timeout-ms", DEADLINE_MS]


def _records(path: Path) -> list[tuple[str, int, bytes]]:
    return list(read_records(path))


PROCESS_FIELDS = ("result", "clock_now_ns", "child_clock_ns", "child_result")


def compare_recordings(reference: Path, other: Path) -> dict:
    """Message-by-message equality, and for proof.process each field."""
    a, b = _records(reference), _records(other)
    differing = Counter()
    for (ta, la, da), (tb, lb, db) in zip(a, b):
        if (ta, la) != (tb, lb):
            differing["order"] += 1
        elif da != db:
            if ta == "proof.process":
                fa, fb = struct.unpack("<4Q", da), struct.unpack("<4Q", db)
                for name, x, y in zip(PROCESS_FIELDS, fa, fb):
                    if x != y:
                        differing[f"proof.process.{name}"] += 1
            else:
                differing[ta] += 1
    return {"messages": [len(a), len(b)], "identical": a == b,
            "differing": dict(sorted(differing.items()))}


# --- perf analysis ----------------------------------------------------------


def perf_script(data: Path, out: Path, env: dict, *extra: str) -> list:
    command = ["perf", "script", "-i", str(data), "-F",
               "comm,pid,tid,time,ip,sym,dso", "--ns", *extra]
    done = subprocess.run(command, capture_output=True, text=True, env=env)
    with gzip.open(out.with_suffix(".txt.gz"), "wt") as f:
        f.write(done.stdout)
    (out.with_suffix(".stderr.txt")).write_text(done.stderr[-20000:])
    return analyze.parse_perf_script(done.stdout)


def _leaf_counts(samples, functions) -> dict[str, int]:
    leaves = Counter(s.leaf.symbol for s in samples if s.leaf)
    return {f: leaves.get(f, 0) for f in functions}


# The source caller of each hot function.
DIRECT_CALLERS = {
    "silproof_native_spin_even": "native_task",
    "silproof_native_spin_odd": "native_task",
    "silproof_process_spin": "silproof_process_step",
    "silproof_child_spin": "main",
    "silproof_fmu_spin": "fmi3DoStep",
}


def _with_direct_caller(samples, function: str) -> int:
    """Samples of `function` whose second frame is its source caller."""
    caller = DIRECT_CALLERS[function]
    return sum(1 for s in samples if s.leaf and s.leaf.symbol == function
               and len(s.frames) > 1 and s.frames[1].symbol == caller)


def _with_callers(samples, function: str) -> int:
    """Samples of `function` whose chain reaches a frame in another module:
    the unwinder got past the workload library."""
    count = 0
    for s in samples:
        if s.leaf and s.leaf.symbol == function and any(
                f.dso != s.leaf.dso for f in s.frames[1:]):
            count += 1
    return count


def _dso_coverage(samples) -> dict[str, dict[str, int]]:
    coverage: dict[str, Counter] = {}
    for s in samples:
        if s.leaf is None:
            continue
        c = coverage.setdefault(Path(s.leaf.dso).name, Counter())
        c["samples"] += 1
        if s.leaf.symbol is None:
            c["unknown"] += 1
    return {k: dict(v) for k, v in sorted(coverage.items())}


def _native_source_lines(data: Path, out: Path, env: dict) -> int | str:
    """Leaf samples that perf maps to a line of silproof_native.c."""
    try:
        done = subprocess.run(
            ["perf", "script", "-i", str(data), "-G", "-F",
             "ip,sym,srcline"],
            capture_output=True, text=True, env=env, timeout=900)
    except subprocess.TimeoutExpired:
        return "timeout"
    with gzip.open(out, "wt") as f:
        f.write(done.stdout)
    return done.stdout.count("silproof_native.c:")


def _hot_functions() -> list[str]:
    return [f for fs in workload.HOT_FUNCTIONS.values() for f in fs]


def analyze_perf(run: Run, a: workload.Artifacts, env: dict,
                 dwarf: bool) -> dict:
    data = run.dir / "perf.data"
    raw = run.dir / "analysis"
    raw.mkdir()
    hot = _hot_functions()
    intervals = run.intervals()
    result: dict = {"perf_data_bytes": data.stat().st_size,
                    "perf_data_sha256": _sha256(data)}
    # Module identities as perf recorded them.
    listing = subprocess.run(
        ["perf", "buildid-list", "-i", str(data), "--with-hits"],
        capture_output=True, text=True, env=env).stdout
    (raw / "buildid-list.txt").write_text(listing)
    modules = analyze.parse_buildid_list(listing)
    expected = {"native": build_id(a.native), "process": build_id(a.process),
                "child": build_id(a.child), "fmu": build_id(a.fmu_binary)}
    result["build_ids"] = {
        name: {"expected": bid,
               "recorded_paths": sorted(p for p, b in modules.items()
                                        if b == bid)}
        for name, bid in expected.items()}
    mmaps = subprocess.run(
        ["perf", "script", "-i", str(data), "--show-mmap-events", "-F",
         "comm,pid,tid,time", "--ns"], capture_output=True, text=True,
        env=env).stdout
    mmap_lines = [line for line in mmaps.splitlines()
                  if "MMAP" in line and ("silproof" in line
                                         or "EgoMotion" in line)]
    (raw / "mmap-events.txt").write_text("\n".join(mmap_lines) + "\n")
    result["fmu_mapped_paths"] = sorted({
        line.rsplit(" ", 1)[-1] for line in mmap_lines
        if "EgoMotion" in line})
    lost = subprocess.run(
        ["perf", "report", "-i", str(data), "--stats"], capture_output=True,
        text=True, env=env).stdout
    (raw / "report-stats.txt").write_text(lost)

    # Layout 1: as recorded, no debug file beside the stripped library.
    as_recorded = perf_script(data, raw / "script-recorded", env)
    result["recorded_layout"] = {
        "samples": len(as_recorded),
        "leaf_counts": _leaf_counts(as_recorded, hot),
        "dso_coverage": _dso_coverage(as_recorded)}
    # The FMU library lived in the Run working directory, which the kernel
    # removed. Adding the built copy (same build ID) to perf's build-ID
    # cache is the explicit capture that a later analysis needs. The
    # layouts below include it.
    subprocess.run(["perf", "buildid-cache", "-a", str(a.fmu_binary)],
                   capture_output=True, text=True, env=env)
    debug_beside = a.native.parent / a.native_debug.name
    layouts = {}
    for name, debug in (("matching", a.native_debug),
                        ("mismatched", a.native_mismatched_debug)):
        shutil.copy2(debug, debug_beside)
        try:
            samples = perf_script(data, raw / f"script-{name}", env, "-v")
            result[f"{name}_debug_source_lines"] = _native_source_lines(
                data, raw / f"srcline-{name}.txt.gz", env)
        finally:
            debug_beside.unlink()
        layouts[name] = samples
        result[f"{name}_debug_layout"] = {
            "samples": len(samples),
            "leaf_counts": _leaf_counts(samples, hot),
            "dso_coverage": _dso_coverage(samples)}
    samples = layouts["matching"]
    result["fmu_after_explicit_cache"] = _leaf_counts(
        samples, ["silproof_fmu_spin"])
    result["callers"] = {f: {"samples": c, "with_caller_outside_module":
                             _with_callers(samples, f),
                             "with_direct_caller":
                             _with_direct_caller(samples, f)}
                         for f, c in _leaf_counts(samples, hot).items()}
    result["join"] = {f: vars(c) for f, c in
                      analyze.join_samples(samples, intervals).items()}
    sample_pids = Counter(s.pid for s in samples)
    result["pids"] = {
        f"{i.role}:{i.pid}": sample_pids.get(i.pid, 0)
        for i in {(i.role, i.pid): i for i in intervals}.values()}
    result["comms"] = dict(Counter(s.comm for s in samples).most_common(20))
    result["dwarf"] = dwarf
    return result


# --- Heaptrack --------------------------------------------------------------


class Interpreters:
    """heaptrack_interpret processes that the driver owns, one per FIFO."""

    def __init__(self, interpret: Path, directory: Path, names: list[str]):
        self.directory = directory
        self.procs = {}
        for name in names:
            fifo = directory / f"{name}.fifo"
            os.mkfifo(fifo)
            self.procs[name] = subprocess.Popen(
                ["sh", "-c", 'exec "$0" < "$1" > "$2"', str(interpret),
                 str(fifo), str(directory / f"{name}.heaptrack")],
                stderr=open(directory / f"{name}.interpret.stderr", "wb"))

    def finish(self) -> dict[str, int | str]:
        status = {}
        for name, proc in self.procs.items():
            fifo = self.directory / f"{name}.fifo"
            # A participant that never started leaves the interpreter in
            # open(); a write end that opens and closes ends it.
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            try:
                status[name] = proc.wait(timeout=COLLECTOR_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                status[name] = "killed after timeout"
            fifo.unlink()
        return status


def interpret_raw(interpret: Path, raw: Path, out: Path) -> int:
    with open(raw, "rb") as source, open(out, "wb") as sink:
        return subprocess.run([str(interpret)], stdin=source, stdout=sink,
                              stderr=open(out.with_suffix(".stderr"), "wb")
                              ).returncode


def heap_summary(path: Path, wall_ns: int) -> dict:
    text = path.read_text(errors="replace") if path.exists() else ""
    capture = analyze.parse_heaptrack(text)
    sites = {}
    for site, (calls, size) in workload.ALLOCATION_SITES.items():
        count = capture.allocations_through(site)
        sites[site] = {**vars(count), "sizes": sorted(
            capture.sizes_through(site)), "expected_calls": calls,
            "expected_size": size}
    stamps = capture.timestamps_ms
    return {
        "bytes": path.stat().st_size if path.exists() else None,
        "sha256": _sha256(path) if path.exists() else None,
        "file_version": capture.file_version, "exe": capture.exe,
        "modules": sorted(m for m in capture.modules()
                          if "silproof" in m or "EgoMotion" in m),
        "sites": sites,
        "timestamps": {
            "count": len(stamps),
            "first_ms": stamps[0] if stamps else None,
            "last_ms": stamps[-1] if stamps else None,
            "distinct": len(set(stamps)),
            # Heaptrack writes elapsed ms as an unsigned number; a clock
            # that went back shows as a value above 2**63.
            "negative": sum(1 for v in stamps if v >= 2 ** 63),
            # True when the stamps can be host time since the tracker
            # started: none negative, none decreasing, none after the Run.
            "host_time_plausible": bool(stamps) and all(
                v < 2 ** 63 for v in stamps) and stamps == sorted(stamps)
                and stamps[-1] <= wall_ns // 1_000_000},
        "rss_samples": capture.rss_samples,
        "run_wall_ms": wall_ns // 1_000_000,
        "allocations_by_size": {
            str(size): sum(capture.allocated[i] for i, (s, _) in
                           enumerate(capture.infos) if s == size)
            for size in (1024, 2048, 4096, 65536)},
    }


# --- the proof --------------------------------------------------------------


class Checks:
    def __init__(self):
        self.items: list[dict] = []

    def gate(self, name: str, passed: bool, detail=None) -> None:
        self.items.append({"name": name, "kind": "check",
                           "passed": bool(passed), "detail": detail})

    def finding(self, name: str, holds: bool, detail=None) -> None:
        self.items.append({"name": name, "kind": "finding",
                           "holds": bool(holds), "detail": detail})

    def failed(self) -> list[str]:
        return [c["name"] for c in self.items
                if c["kind"] == "check" and not c["passed"]]


HEAPTRACK_RUNS = ["heaptrack-runner", "heaptrack-runner-mismatched",
                  "heaptrack-fmu", "heaptrack-shimmed-process",
                  "heaptrack-shimmed-process-sleep-immediate"]


def require_backend(checks: Checks, runs: dict, probes: dict, probe_name: str,
                    names: list[str]) -> bool:
    """A backend that its probe found unavailable qualifies nothing, so the
    proof fails: its Runs are skipped with the probe outcome."""
    outcome = probes[probe_name]["outcome"]
    available = outcome == "available"
    checks.gate(f"{probe_name}: the backend is available, so its Runs are "
                "made", available, outcome)
    if not available:
        for name in names:
            runs[name] = {"skipped": f"{probe_name} probe outcome {outcome}"}
    return available


def run_proof(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    facts = probe(out / "probe")
    artifacts = workload.build(out / "build")
    manifest = workload.manifest(artifacts).write(out / "manifest.json")
    preload, interpret = heaptrack_paths()
    checks = Checks()
    results: dict = {"host": facts["host"], "probes": facts["probes"],
                     "settings": {"event": PERF_EVENT,
                                  "frequency_hz": int(PERF_FREQUENCY),
                                  "clock": PERF_CLOCK,
                                  "dwarf_stack_bytes": int(DWARF_STACK),
                                  "participant_timeout_ms": int(DEADLINE_MS),
                                  "cflags": workload.CFLAGS,
                                  "duration_ns": workload.DURATION_NS,
                                  "period_ns": workload.PERIOD_NS},
                     "artifacts": artifact_identities(artifacts),
                     "manifest_sha256": _sha256(manifest.path),
                     "runs": {}}
    markers = [str(out / "runs"), str(out / "build"),
               str(out / "manifest.json"), "heaptrack_interpret",
               "silproof_child"]

    def finish(run: Run, code: int, exit_gate: bool = True) -> None:
        run.result["leftover_processes"] = _leftovers(markers)
        results["runs"][run.name] = run.result
        (checks.gate if exit_gate else checks.finding)(
            f"{run.name}: Run exits 0", code == 0, code)
        checks.gate(f"{run.name}: no process is left", not
                    run.result["leftover_processes"],
                    run.result["leftover_processes"])

    ordinary = Run(out, "ordinary")
    finish(ordinary, ordinary.execute(
        sil_run_command(manifest.path, ordinary),
        _clean_env(SILPROOF_SIDECAR_DIR=str(ordinary.sidecar))))
    expected_intervals = Counter(i.function for i in ordinary.intervals())
    checks.gate("ordinary: one interval per hot call", expected_intervals == {
        "silproof_native_spin_even": workload.ACTIVATIONS // 2,
        "silproof_native_spin_odd": workload.ACTIVATIONS // 2,
        "silproof_process_spin": workload.ACTIVATIONS,
        "silproof_fmu_spin": workload.ACTIVATIONS,
        "silproof_child_spin": workload.CHILDREN}, dict(expected_intervals))

    perf_available = require_backend(checks, results["runs"],
                                     facts["probes"], "perf_user_dwarf",
                                     ["perf-dwarf", "perf-fp"])
    # perf-dwarf records build IDs in the mmap events (--buildid-mmap);
    # perf-fp keeps perf's default, which reads them from the files when
    # the recording ends.
    for name, call_graph, dwarf, identity in (
            ("perf-dwarf", f"dwarf,{DWARF_STACK}", True, ["--buildid-mmap"]),
            ("perf-fp", "fp", False, [])):
        if not perf_available:
            continue
        run = Run(out, name)
        env = _clean_env(SILPROOF_SIDECAR_DIR=str(run.sidecar),
                         PERF_BUILDID_DIR=str(run.dir / "buildid"))
        command = ["perf", "record", "-e", PERF_EVENT, "-F", PERF_FREQUENCY,
                   "-k", PERF_CLOCK, "--call-graph", call_graph, *identity,
                   "-o",
                   str(run.dir / "perf.data"), "--",
                   *sil_run_command(manifest.path, run)]
        finish(run, run.execute(command, env))
        run.result["recording"] = compare_recordings(ordinary.recording,
                                                     run.recording)
        checks.gate(f"{name}: Recording equals the ordinary Recording",
                    run.result["recording"]["identical"],
                    run.result["recording"])
        perf = analyze_perf(run, artifacts, env, dwarf)
        perf["buildid_mmap"] = bool(identity)
        run.result["perf"] = perf
        _perf_checks(checks, name, perf, dwarf)

    if require_backend(checks, results["runs"], facts["probes"],
                       "heaptrack_preload", HEAPTRACK_RUNS):
        _heaptrack_runs(out, manifest.path, artifacts, preload, interpret,
                        ordinary, results, checks, finish)
    results["checks"] = checks.items
    results["failed_checks"] = checks.failed()
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


def _perf_checks(checks: Checks, name: str, perf: dict, dwarf: bool) -> None:
    matching = perf["matching_debug_layout"]["leaf_counts"]
    mismatched = perf["mismatched_debug_layout"]["leaf_counts"]
    recorded = perf["recorded_layout"]["leaf_counts"]
    native = workload.HOT_FUNCTIONS["native"]
    for function, count in matching.items():
        fmu = function == "silproof_fmu_spin"
        (checks.gate if not fmu or perf["buildid_mmap"] else checks.finding)(
            f"{name}: samples name {function} (matching debug file)",
            count > 0, count)
    checks.gate(f"{name}: the matching debug file gives source lines",
                isinstance(perf["matching_debug_source_lines"], int)
                and perf["matching_debug_source_lines"] > 0,
                perf["matching_debug_source_lines"])
    checks.gate(f"{name}: a mismatched debug file gives no source line",
                perf["mismatched_debug_source_lines"] == 0,
                perf["mismatched_debug_source_lines"])
    checks.gate(f"{name}: a mismatched debug file names no stripped "
                "function", all(mismatched[f] == 0 for f in native),
                {f: mismatched[f] for f in native})
    checks.finding(f"{name}: the stripped library alone names its static "
                   "functions", any(recorded[f] for f in native),
                   {f: recorded[f] for f in native})
    for function, join in perf["join"].items():
        checks.gate(f"{name}: every {function} sample lies in one of its "
                    "own host intervals", join["outside"] == 0
                    and join["inside_other_function"] == 0, join)
    checks.gate(f"{name}: every workload process has samples under its own "
                "PID", all(v > 0 for v in perf["pids"].values()),
                perf["pids"])
    for module, ids in perf["build_ids"].items():
        # A file that is gone when the recording ends has no build ID
        # unless the mmap events carry it.
        (checks.gate if module != "fmu" or perf["buildid_mmap"]
         else checks.finding)(f"{name}: perf recorded the {module} build ID",
                              bool(ids["recorded_paths"]), ids)
    checks.finding(f"{name}: the FMU library resolves after the Run "
                   "directory is gone, with no explicit capture",
                   recorded["silproof_fmu_spin"] > 0,
                   recorded["silproof_fmu_spin"])
    (checks.gate if perf["buildid_mmap"] else checks.finding)(
                f"{name}: the FMU library resolves after an explicit "
                "build-ID cache capture",
                perf["fmu_after_explicit_cache"]["silproof_fmu_spin"] > 0,
                perf["fmu_after_explicit_cache"])
    for function, c in perf["callers"].items():
        record = checks.gate if dwarf else checks.finding
        record(f"{name}: {function} samples unwind past their module",
               c["with_caller_outside_module"] > 0, c)
        record(f"{name}: {function} samples show its source caller "
               f"{DIRECT_CALLERS[function]}", c["with_direct_caller"] > 0, c)
        checks.finding(f"{name}: every {function} sample shows its source "
                       "caller", c["samples"] > 0
                       and c["with_direct_caller"] == c["samples"], c)


def _heaptrack_runs(out, manifest_path, artifacts, preload, interpret,
                    ordinary, results, checks, finish) -> None:
    debug_beside = artifacts.native.parent / artifacts.native_debug.name
    for name, debug in (("heaptrack-runner", artifacts.native_debug),
                        ("heaptrack-runner-mismatched",
                         artifacts.native_mismatched_debug)):
        run = Run(out, name)
        collectors = Interpreters(interpret, run.dir, ["runner"])
        shutil.copy2(debug, debug_beside)
        try:
            code = run.execute(
                [str(LAUNCH), "live", "runner",
                 *sil_run_command(manifest_path, run)],
                _clean_env(SILPROOF_SIDECAR_DIR=str(run.sidecar),
                           SILPROOF_HEAPTRACK_DIR=str(run.dir),
                           SILPROOF_HEAPTRACK_PRELOAD=str(preload)))
            run.result["collectors"] = collectors.finish()
        finally:
            debug_beside.unlink()
        finish(run, code)
        run.result["recording"] = compare_recordings(ordinary.recording,
                                                     run.recording)
        checks.gate(f"{name}: Recording equals the ordinary Recording",
                    run.result["recording"]["identical"],
                    run.result["recording"])
        checks.gate(f"{name}: the interpreter exits 0",
                    run.result["collectors"] == {"runner": 0},
                    run.result["collectors"])
        heap = heap_summary(run.dir / "runner.heaptrack",
                            run.result["wall_ns"])
        run.result["heaptrack"] = {"runner": heap}
        sites = heap["sites"]
        checks.gate(f"{name}: the runner capture holds 1024-byte "
                    "allocations from the Native library",
                    heap["allocations_by_size"]["1024"] >=
                    workload.ALLOCATION_SITES[
                        "silproof_native_alloc_site"][0],
                    heap["allocations_by_size"])
        if name == "heaptrack-runner":
            for site in ("silproof_native_alloc_site",
                         "silproof_native_retain_site"):
                s = sites[site]
                checks.gate(f"{name}: {site} has its exact calls and size",
                            s["calls"] == s["expected_calls"]
                            and s["sizes"] == [s["expected_size"]], s)
            checks.finding(f"{name}: the runner's Heaptrack timestamps can be "
                           "host time", heap["timestamps"][
                               "host_time_plausible"], heap["timestamps"])
            retained = sites["silproof_native_retain_site"]
            checks.gate(f"{name}: the retained block is outstanding at "
                        "capture end", retained["outstanding_calls"] == 1,
                        retained)
            checks.gate(f"{name}: no Process participant allocation is in "
                        "the runner capture",
                        sites["silproof_process_alloc_site"]["calls"] == 0
                        and sites["silproof_fmu_alloc_site"]["calls"] == 0,
                        {k: sites[k]["calls"] for k in (
                            "silproof_process_alloc_site",
                            "silproof_fmu_alloc_site")})
        else:
            checks.gate(f"{name}: a mismatched debug file names no "
                        "stripped allocation site",
                        sites["silproof_native_alloc_site"]["calls"] == 0,
                        sites["silproof_native_alloc_site"])

    # The FMU group's Process participant, unshimmed: the qualified
    # Process scope.
    _heaptrack_scope(out, artifacts, preload, interpret, ordinary, checks,
                     finish, "heaptrack-fmu", live=["fmu"], raw=[],
                     sleep="reject", qualified=True)
    # The shimmed Process participant, with the shim's default and other
    # sleep policy. The workload never sleeps; Heaptrack's own threads do.
    # Findings only: this scope is not qualified. The second Run also
    # records the FMU raw and interprets it after the Run.
    _heaptrack_scope(out, artifacts, preload, interpret, ordinary, checks,
                     finish, "heaptrack-shimmed-process", live=["process"],
                     raw=[], sleep="reject", qualified=False)
    _heaptrack_scope(out, artifacts, preload, interpret, ordinary, checks,
                     finish, "heaptrack-shimmed-process-sleep-immediate",
                     live=["process"], raw=["fmu"], sleep="immediate",
                     qualified=False)


# The allocation site of each Process participant's own code.
PARTICIPANT_SITES = {"process": "silproof_process_alloc_site",
                     "fmu": "silproof_fmu_alloc_site"}


def _heaptrack_scope(out, artifacts, preload, interpret, ordinary, checks,
                     finish, name, *, live, raw, sleep, qualified) -> None:
    """One Run with Heaptrack launched into the named Process
    participants: `live` through a FIFO and a driver-owned interpreter,
    `raw` to a file that the driver interprets after the Run."""
    gate = checks.gate if qualified else checks.finding
    run = Run(out, name)
    prefix = {n: [str(LAUNCH), "live", n] for n in live}
    prefix.update({n: [str(LAUNCH), "raw", n] for n in raw})
    wrapped = workload.manifest(artifacts, process_sleep=sleep,
                                launch_prefix=prefix).write(
                                    run.dir / "manifest.json")
    collectors = Interpreters(interpret, run.dir, live)
    code = run.execute(sil_run_command(wrapped.path, run), _clean_env(
        SILPROOF_SIDECAR_DIR=str(run.sidecar),
        SILPROOF_HEAPTRACK_DIR=str(run.dir),
        SILPROOF_HEAPTRACK_PRELOAD=str(preload)))
    run.result["collectors"] = collectors.finish()
    run.result["process_sleep"] = sleep
    run.result["scopes"] = {"live": live, "raw": raw}
    late = {}
    for n in raw:
        file = run.dir / f"{n}.raw"
        late[n] = {"raw_bytes": file.stat().st_size if file.exists()
                   else None, "interpret_returncode": interpret_raw(
                       interpret, file, run.dir / f"{n}.heaptrack")
                   if file.exists() else None}
    run.result["late_interpretation"] = late
    finish(run, code, exit_gate=qualified)
    stderr = (run.dir / "stderr.txt").read_text(errors="replace")
    run.result["stderr_tail"] = stderr[-2000:]
    comparison = compare_recordings(ordinary.recording, run.recording) \
        if run.recording.exists() else {"messages": [None, 0],
                                        "identical": False, "differing": {}}
    run.result["recording"] = comparison
    if "process" in live + raw:
        shim_fields = {k: v for k, v in comparison["differing"].items()
                       if k != "proof.process.child_clock_ns"}
        gate(f"{name}: Recording equals the ordinary Recording except the "
             "child's clock reading",
             comparison["messages"][0] == comparison["messages"][1]
             and not shim_fields, comparison)
        checks.finding(f"{name}: the short-lived child keeps the Clock shim",
                       comparison["identical"], comparison["differing"])
    else:
        gate(f"{name}: Recording equals the ordinary Recording",
             comparison["identical"], comparison)
    gate(f"{name}: the interpreter exits 0",
         run.result["collectors"] == {n: 0 for n in live},
         run.result["collectors"])
    summaries = {}
    for n in live:
        heap = heap_summary(run.dir / f"{n}.heaptrack", run.result["wall_ns"])
        summaries[n] = heap
        site = heap["sites"][PARTICIPANT_SITES[n]]
        gate(f"{name}: {PARTICIPANT_SITES[n]} has its exact calls and size",
             site["calls"] == site["expected_calls"]
             and site["sizes"] == [site["expected_size"]], site)
        others = [s for s in workload.ALLOCATION_SITES
                  if s != PARTICIPANT_SITES[n]]
        gate(f"{name}: no other participant's allocation is in the {n} "
             "capture", all(heap["sites"][s]["calls"] == 0 for s in others),
             {s: heap["sites"][s]["calls"] for s in others})
        checks.finding(f"{name}: the {n} participant's Heaptrack timestamps "
                       "can be host time",
                       heap["timestamps"]["host_time_plausible"],
                       heap["timestamps"])
    for n in raw:
        heap = heap_summary(run.dir / f"{n}.heaptrack", run.result["wall_ns"])
        summaries[f"{n}_late"] = heap
        site = PARTICIPANT_SITES[n]
        calls, size = workload.ALLOCATION_SITES[site]
        checks.finding(f"{name}: {site} resolves when the raw data is "
                       "interpreted after the Run directory is gone",
                       heap["sites"][site]["calls"] > 0, heap["sites"][site])
        gate(f"{name}: the late {n} capture keeps every {size}-byte "
             "allocation", heap["allocations_by_size"][str(size)] >= calls,
             heap["allocations_by_size"])
    run.result["heaptrack"] = summaries


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("probe", "run"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    out = Path(argv[1]).resolve()
    if argv[0] == "probe":
        result = probe(out)
        print(json.dumps({k: v["outcome"] for k, v in
                          result["probes"].items()}))
        return 0
    results = run_proof(out)
    import report  # noqa: PLC0415
    (out / "results.md").write_text(report.markdown(results))
    for c in results["checks"]:
        state = ("PASS" if c["passed"] else "FAIL") if c["kind"] == "check" \
            else ("holds" if c["holds"] else "does not hold")
        print(f"{c['kind']:8} {state:13} {c['name']}")
    return 1 if results["failed_checks"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
