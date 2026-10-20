"""Qualification of the installed `sil profile` workflows (#319).

    qualify.py build WORK
    qualify.py run WORK EVIDENCE --environment NAME --perf available|denied
                   [--group NAME]... [--small-dir DIR]
    qualify.py render SETS EVIDENCE

`build` compiles the workload into WORK/workload (fixtures.py). It needs a
compiler; the qualified environment does not.

`run` uses only the installed `sil` on PATH and the Python that runs this
file. It authors the Manifests, then runs each scenario of the selected
groups as a user does: one command, its exit code, stdout, stderr and files.
It writes EVIDENCE/results.json and results.md, and exits 1 when a check
fails. A check never uses a time, a sample share, an RSS value or an
overhead: wall times and sizes are observations only. `--perf` states what
the environment allows, so a denied environment is checked for its
actionable preflight outcome.

`render` renders the copied portable sets in SETS with `sil profile render`,
in an environment that has no perf, no Heaptrack and no profiled binary.

Groups: preflight, equivalence, cpu, heap, storage (needs --small-dir, a
small file system for real storage exhaustion).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import fixtures

GROUPS = ("preflight", "equivalence", "cpu", "heap", "storage")
DEADLINE = ["--participant-timeout-ms", "60000"]
SHORT_DEADLINE = ["--participant-timeout-ms", "1000"]
MARKERS = ("silproof", "probe_participant", "can_observer", "sil.fmi",
           "process_adapter", "sil-run-diagnostics", "heaptrack_interpret",
           "perf record", "launch-adapter")
TAIL = 4096
HOT = {"native": ("silproof_native_spin_even", "silproof_native_spin_odd"),
       "process": ("silproof_process_spin",),
       "child": ("silproof_child_spin",),
       "fmu": ("silproof_fmu_spin",)}
NATIVE_SITES = {"silproof_native_alloc_site": (1600, 1024 * 1600),
                "silproof_native_retain_site": (1, 65536)}
FMU_SITE = ("silproof_fmu_alloc_site", 400, 400 * 4096)
RAW_FILES = ("cpu/perf.data", "heap/heaptrack.data",
             "activation/observations.jsonl", "run.mcap")
SET_FILES = ("profile.json", "cpu/samples.json", "activation/correlation.json",
             "heap/allocations.json")


# --- facts -------------------------------------------------------------------


def _text(command: list[str]) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (done.stdout or done.stderr).strip().splitlines()[0] \
        if (done.stdout or done.stderr).strip() else None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def environment_facts() -> dict:
    import sil
    status = {}
    for line in (_read("/proc/self/status") or "").splitlines():
        key, _, value = line.partition(":")
        if key in ("CapEff", "CapBnd", "Seccomp", "NSpid"):
            status[key] = value.strip()
    return {
        "uname": platform.uname()._asdict(),
        "libc": " ".join(platform.libc_ver()),
        "python": platform.python_version(),
        "uid": os.getuid(),
        "in_container": Path("/.dockerenv").exists(),
        "perf_event_paranoid": _read("/proc/sys/kernel/perf_event_paranoid"),
        "hardware_pmu": Path("/sys/bus/event_source/devices/cpu").exists(),
        "status": status,
        "sil_package": str(Path(sil.__file__).parent),
        "sil_version": _text(["sil-run", "--version"]),
        "sil_build_info": _text(["sil-run", "--build-info"]),
        "tools": {name: shutil.which(name) for name in
                  ("sil", "sil-run", "sil-run-diagnostics",
                   "sil-diagnostics-launcher", "perf", "heaptrack")},
        "perf_version": _text(["perf", "--version"]),
        "perf_capabilities": _text(["getcap", os.path.realpath(
            shutil.which("perf"))]) if shutil.which("perf") and
        shutil.which("getcap") else None,
        "heaptrack_version": _text(["heaptrack", "--version"]),
        "packages": _packages(),
    }


def _packages() -> list[str]:
    if not shutil.which("dpkg-query"):
        return []
    done = subprocess.run(
        ["dpkg-query", "-W", "-f", "${Package}=${Version}\\n",
         "linux-perf", "linux-tools-common", f"linux-tools-{os.uname().release}",
         "heaptrack", "libcap2-bin"], capture_output=True, text=True)
    return sorted(line for line in done.stdout.splitlines() if line)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _leftovers() -> list[str]:
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            text = (proc / "cmdline").read_bytes().replace(b"\0", b" ") \
                .decode(errors="replace").strip()
        except OSError:
            continue
        if text and any(marker in text for marker in MARKERS):
            found.append(f"{proc.name}: {text[:200]}")
    return found


def _wait_no_leftovers(seconds: float = 10) -> list[str]:
    deadline = time.monotonic() + seconds
    while True:
        found = _leftovers()
        if not found or time.monotonic() > deadline:
            return found
        time.sleep(0.2)


def _children_of(pid: int) -> list[tuple[int, str]]:
    """Every descendant of `pid` with its command line."""
    parents: dict[int, list[int]] = {}
    commands = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            stat = (proc / "stat").read_text()
            commands[int(proc.name)] = (proc / "cmdline").read_bytes() \
                .replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        parents.setdefault(ppid, []).append(int(proc.name))
    found, todo = [], [pid]
    while todo:
        for child in parents.get(todo.pop(), []):
            found.append((child, commands.get(child, "")))
            todo.append(child)
    return found


# --- one scenario ------------------------------------------------------------


class Qualification:
    def __init__(self, work: Path, evidence: Path, environment: str,
                 perf: str):
        self.workload = work / "workload"
        self.work = work / "runs" / environment
        self.evidence = evidence
        self.environment = environment
        self.perf = perf
        self.sidecar = self.work / "sidecar"
        self.tmp = self.work / "tmp"
        self.scenarios: list[dict] = []
        self.failed = 0
        for d in (self.sidecar, self.tmp, evidence):
            d.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ, "SILPROOF_SIDECAR_DIR": str(self.sidecar),
                    "SILQUAL_SIDECAR_DIR": str(self.sidecar),
                    "TMPDIR": str(self.tmp)}
        self.env.pop("DEBUGINFOD_URLS", None)

    # Each scenario has its own working directory with a sentinel file. The
    # command runs there, as a user runs it in a project directory.
    def scenario(self, name: str, argv: list[str], *, env: dict | None = None,
                 action=None, timeout: int = 1800, outputs=("prof",)) -> dict:
        cwd = self.work / "scenarios" / name
        cwd.mkdir(parents=True)
        sentinel = cwd / "unrelated.txt"
        sentinel.write_text(f"unrelated file of {name}\n")
        sentinel_digest = sha256(sentinel)
        for entry in self.sidecar.iterdir():
            entry.unlink()
        tmp_before = sorted(p.name for p in self.tmp.iterdir())
        started = time.monotonic_ns()
        process = subprocess.Popen(argv, cwd=cwd, env=env or self.env,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        acted = action(process) if action else None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            timed_out = True
        wall_ns = time.monotonic_ns() - started
        record = {
            "name": name, "argv": argv, "exit_code": process.returncode,
            "timed_out": timed_out, "wall_ms": wall_ns // 1_000_000,
            "action": acted,
            "stdout_tail": stdout[-TAIL:].decode(errors="replace"),
            "stderr_tail": stderr[-TAIL:].decode(errors="replace"),
            "leftover_processes": _wait_no_leftovers(),
            "sentinel_unchanged": sentinel.exists() and
            sha256(sentinel) == sentinel_digest,
            "new_entries": sorted(p.name for p in cwd.iterdir()
                                  if p.name != "unrelated.txt"),
            "tmp_new_entries": sorted(set(p.name for p in self.tmp.iterdir())
                                      - set(tmp_before)),
            "participants_started": sorted(p.name for p in
                                           self.sidecar.iterdir()),
            "checks": [], "observations": {},
        }
        record["cwd"] = str(cwd)
        probes = sorted(self.sidecar.glob("probe-*.json"))
        if probes:
            record["probe"] = [json.loads(p.read_text()) for p in probes]
        self.scenarios.append(record)
        return record

    def check(self, record: dict, name: str, ok: bool, detail=None) -> bool:
        record["checks"].append({"check": name, "ok": bool(ok),
                                 "detail": detail})
        if not ok:
            self.failed += 1
        return ok

    def clean(self, record: dict, outputs: set[str]) -> None:
        """Normal cleanup: no process is left, no unrelated file changed,
        and the command made only the outputs it was asked for."""
        self.check(record, "no process left", not record["leftover_processes"],
                   record["leftover_processes"])
        self.check(record, "unrelated file unchanged",
                   record["sentinel_unchanged"])
        self.check(record, "only the requested outputs are new",
                   set(record["new_entries"]) <= outputs,
                   record["new_entries"])
        self.check(record, "no file left in TMPDIR",
                   not record["tmp_new_entries"], record["tmp_new_entries"])

    def write(self, facts: dict, extra: dict | None = None) -> None:
        result = {"environment": self.environment, "perf": self.perf,
                  "facts": facts, "failed_checks": self.failed,
                  "scenarios": self.scenarios, **(extra or {})}
        (self.evidence / "results.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n")
        (self.evidence / "results.md").write_text(_markdown(result))


def _markdown(result: dict) -> str:
    lines = [f"# Profiling qualification: {result['environment']}", "",
             f"perf: {result['perf']}; failed checks: "
             f"{result['failed_checks']}", "",
             "| Scenario | Exit | Wall ms | Checks | Failed |",
             "| --- | --- | --- | --- | --- |"]
    for s in result["scenarios"]:
        failed = [c["check"] for c in s["checks"] if not c["ok"]]
        lines.append(f"| {s['name']} | {s['exit_code']} | {s['wall_ms']} | "
                     f"{len(s['checks'])} | {'; '.join(failed) or '-'} |")
    lines += ["", "## Observations", ""]
    for s in result["scenarios"]:
        if s["observations"]:
            lines.append(f"- {s['name']}: " + json.dumps(
                s["observations"], sort_keys=True))
    return "\n".join(lines) + "\n"


# --- what a profile directory says ------------------------------------------


def _profile(record: dict) -> dict | None:
    path = Path(record["cwd"]) / "prof" / "profile.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _sizes(directory: Path) -> dict[str, int]:
    return {str(p.relative_to(directory)): p.stat().st_size
            for p in sorted(directory.rglob("*")) if p.is_file()}


def _self_symbols(report: dict) -> dict[str, int]:
    capture = report["capture"]
    rows = (capture["analysis"]["hotspots"]["self"]
            if capture["kind"] == "heap" else capture["hotspots"]["self"])
    key = "allocations" if capture["kind"] == "heap" else "self_samples"
    return {row["symbol"]: row[key] for row in rows if row["symbol"]}


def _heap_rows(report: dict) -> dict[str, dict]:
    return {row["symbol"]: row
            for row in report["capture"]["analysis"]["hotspots"]["self"]
            if row["symbol"]}


def _module(report: dict, suffix: str) -> dict | None:
    modules = (report["capture"]["analysis"]["modules"]
               if report["capture"]["kind"] == "heap"
               else report["capture"]["modules"])
    for module in modules:
        if module["path"].endswith(suffix):
            return module
    return None


def _observe(q: Qualification, record: dict, report: dict | None) -> None:
    out = Path(record["cwd"]) / "prof"
    record["observations"]["wall_ms"] = record["wall_ms"]
    if out.is_dir():
        record["observations"]["bytes"] = _sizes(out)
        # The raw evidence stays in the work directory; its digest is here.
        record["observations"]["raw_sha256"] = {
            name: sha256(out / name) for name in RAW_FILES
            if (out / name).is_file()}
    if report is not None:
        record["observations"]["run"] = report["run"]["status"]
        record["observations"]["capture"] = [report["capture"]["status"],
                                             report["capture"]["reasons"]]
        correlation = report["activation_correlation"]
        if correlation["kind"] != "not_requested":
            record["observations"]["correlation"] = [
                correlation.get("status"), correlation.get("reasons")]
        totals = report["capture"].get("totals") or (
            (report["capture"].get("analysis") or {}).get("totals"))
        record["observations"]["totals"] = totals
        keep = q.evidence / "profiles" / record["name"]
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out / "profile.json", keep / "profile.json")


def _keep_set(q: Qualification, record: dict) -> None:
    """Copy the portable set, and only it, for the offline render."""
    out = Path(record["cwd"]) / "prof"
    target = q.evidence / "sets" / record["name"]
    for name in SET_FILES:
        if (out / name).is_file():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out / name, target / name)


# --- scenario groups ----------------------------------------------------------


class Runs:
    """The Manifests and the reference Runs of one environment."""

    def __init__(self, q: Qualification):
        self.q = q
        built, raw = fixtures.load(q.workload)
        self.built, self.raw = built, raw
        self.debug_beside = built.native.parent / built.native_debug.name
        manifests = q.work / "manifests"
        main = fixtures.main_only(built, manifests)
        self.with_debug("matching")
        reference = manifests / "main-reference.mcap"
        if not reference.exists():
            subprocess.run(["sil", "run", str(main), "-o", str(reference),
                            *DEADLINE], check=True, env=q.env,
                           cwd=manifests, capture_output=True)
        self.manifests = fixtures.manifests(built, raw, reference, manifests)
        self.recordings: dict[str, Path] = {}
        self.outcomes: dict[str, dict] = {}

    def with_debug(self, variant: str) -> None:
        """The separate debug file beside the stripped Native library:
        matching, mismatched (same name, other build ID) or missing."""
        self.debug_beside.unlink(missing_ok=True)
        if variant == "matching":
            shutil.copy2(self.built.native_debug, self.debug_beside)
        elif variant == "mismatched":
            shutil.copy2(self.built.native_mismatched_debug,
                         self.debug_beside)

    def sil(self, *argv: str) -> list[str]:
        return ["sil", *argv]


def _deadline_for(manifest: str) -> list[str]:
    return SHORT_DEADLINE if manifest == "deadline" else DEADLINE


def _failure_category(path: Path) -> str | None:
    failure = Path(f"{path}.failure.json")
    if not failure.is_file():
        return None
    return json.loads(failure.read_text()).get("category")


def equivalence(q: Qualification, runs: Runs) -> None:
    """Ordinary and diagnostics Runs of every deterministic fixture: the
    references of the profiled Runs."""
    runs.with_debug("matching")
    for manifest in ("main", "mixed", "failure", "deadline"):
        path = runs.manifests[manifest]
        for kind, extra in (("ordinary", []), ("diagnostics",
                                               ["--diagnostics"])):
            name = f"{kind}-{manifest}"
            record = q.scenario(name, runs.sil(
                "run", str(path), "-o", "run.mcap", *_deadline_for(manifest),
                *extra))
            cwd = Path(record["cwd"])
            recording = cwd / "run.mcap"
            outcome = {"exit_code": record["exit_code"],
                       "category": _failure_category(recording),
                       "recording": sha256(recording)
                       if recording.is_file() else None}
            runs.outcomes[name] = outcome
            record["observations"] = {"wall_ms": record["wall_ms"],
                                      **outcome}
            expected = 0 if manifest in ("main", "mixed") else 1
            q.check(record, f"exit {expected}", record["exit_code"] ==
                    expected, record["stderr_tail"][-400:])
            q.clean(record, {"run.mcap", "run.mcap.provenance.json",
                             "run.mcap.diagnostics.json",
                             "run.mcap.failure.json"})
        q.check(record, "diagnostics outcome equals ordinary",
                runs.outcomes[f"ordinary-{manifest}"] ==
                runs.outcomes[f"diagnostics-{manifest}"],
                [runs.outcomes[f"ordinary-{manifest}"],
                 runs.outcomes[f"diagnostics-{manifest}"]])


def _same_outcome(q: Qualification, runs: Runs, record: dict,
                  manifest: str, recording: Path) -> None:
    reference = runs.outcomes.get(f"ordinary-{manifest}")
    if reference is None:
        return
    report = _profile(record)
    run = report["run"] if report else {}
    profiled = {"exit_code": run.get("exit_code"),
                "category": (run.get("failure") or {}).get("category"),
                "recording": sha256(recording) if recording.is_file()
                else None}
    q.check(record, "Run outcome and Recording equal the ordinary Run",
            profiled == reference, [profiled, reference])


def _manifest_identity(q: Qualification, record: dict, path: Path,
                       before: str) -> None:
    report = _profile(record)
    q.check(record, "Manifest bytes unchanged", sha256(path) == before)
    if report:
        stdout = record["stdout_tail"]
        hashes = [line.split()[1] for line in stdout.splitlines()
                  if line.startswith("manifest_hash ")]
        q.check(record, "profile identity is the Run's Manifest hash",
                report["identity"]["manifest_hash"] in hashes,
                [report["identity"]["manifest_hash"], hashes])


PROFILE_OUTPUTS = {"prof"}


def preflight(q: Qualification, runs: Runs) -> None:
    main = str(runs.manifests["main"])
    tool_free = {**q.env, "PATH": os.pathsep.join(
        str(Path(shutil.which(t)).parent) for t in ("sil", "sil-run"))}
    for name, collector, missing in (
            ("preflight-cpu-no-perf", ["--collector", "cpu"], "perf"),
            ("preflight-heap-no-heaptrack", ["--collector", "heap",
                                             "--runner"], "heaptrack")):
        record = q.scenario(name, runs.sil("profile", main, "--out", "prof",
                                           *collector, *DEADLINE),
                            env=tool_free)
        q.check(record, f"{missing} not on the tool-free PATH",
                shutil.which(missing, path=tool_free["PATH"]) is None)
        q.check(record, "exit 1 (actionable preflight error)",
                record["exit_code"] == 1, record["stderr_tail"][-400:])
        q.check(record, f"stderr names {missing}",
                missing in record["stderr_tail"])
        q.check(record, "no Participant started",
                not record["participants_started"])
        q.clean(record, set())
    if q.perf == "denied":
        record = q.scenario("preflight-cpu-permission-denied", runs.sil(
            "profile", main, "--out", "prof", "--collector", "cpu",
            *DEADLINE))
        q.check(record, "exit 1 (actionable preflight error)",
                record["exit_code"] == 1, record["stderr_tail"][-600:])
        q.check(record, "stderr names the permission",
                "perf_event_paranoid" in record["stderr_tail"] or
                "CAP_PERFMON" in record["stderr_tail"])
        q.check(record, "no Participant started",
                not record["participants_started"])
        q.clean(record, set())
    for name, scope, code in (
            ("preflight-heap-shimmed-participant", "process", 2),
            ("preflight-heap-native-participant", "native", 2)):
        record = q.scenario(name, runs.sil(
            "profile", main, "--out", "prof", "--collector", "heap",
            "--participant", scope, *DEADLINE))
        q.check(record, f"exit {code}", record["exit_code"] == code,
                record["stderr_tail"][-400:])
        q.check(record, "no Participant started",
                not record["participants_started"])
        q.clean(record, set())
    cwd_name = "preflight-existing-out"
    existing = q.work / "scenarios" / cwd_name / "prof"
    record = q.scenario(cwd_name, ["sh", "-c",
                                   "mkdir prof && echo keep > prof/keep && "
                                   "exec \"$@\"", "sh", *runs.sil(
                                       "profile", main, "--out", "prof",
                                       "--collector", "heap", "--runner",
                                       *DEADLINE)])
    q.check(record, "exit 2 for an existing output directory",
            record["exit_code"] == 2, record["stderr_tail"][-400:])
    q.check(record, "existing evidence unchanged",
            sorted(p.name for p in existing.iterdir()) == ["keep"] and
            (existing / "keep").read_text() == "keep\n")
    q.check(record, "no Participant started",
            not record["participants_started"])


def _wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _after_probe_started(q: Qualification, seconds: float):
    """An action: wait until the probe Participant runs, then `seconds`."""
    def started() -> bool:
        return any(p.name.startswith("probe-") for p in q.sidecar.iterdir())

    def action(process, then):
        ok = _wait_for(started, 120)
        time.sleep(seconds)
        return {"probe_started": ok, **then(process)}
    return action


def _interrupt(q: Qualification):
    act = _after_probe_started(q, 2.0)
    return lambda process: act(process, lambda p: (
        p.send_signal(signal.SIGINT), {"signal": "SIGINT"})[1])


def _kill_descendant(q: Qualification, marker: str):
    act = _after_probe_started(q, 2.0)

    def kill(process):
        victims = [pid for pid, command in _children_of(process.pid)
                   if marker in command]
        for pid in victims[:1]:
            os.kill(pid, signal.SIGKILL)
        return {"killed": marker, "found": len(victims)}
    return lambda process: act(process, kill)


def _profile_checks(q: Qualification, runs: Runs, record: dict,
                    manifest: str, *, run_status: str, capture: str,
                    exit_code: int, reasons: set[str] | None = None) -> dict:
    report = _profile(record)
    _observe(q, record, report)
    q.check(record, f"exit {exit_code}", record["exit_code"] == exit_code,
            record["stderr_tail"][-600:])
    if not q.check(record, "profile.json written and valid",
                   report is not None and _valid(report)):
        return {}
    q.check(record, f"Run status {run_status}",
            report["run"]["status"] == run_status, report["run"])
    q.check(record, f"capture {capture}",
            report["capture"]["status"] == capture,
            report["capture"]["reasons"])
    if reasons is not None:
        q.check(record, f"capture reasons include {sorted(reasons)}",
                reasons <= set(report["capture"]["reasons"]),
                report["capture"]["reasons"])
    q.clean(record, PROFILE_OUTPUTS)
    return report


def _valid(report: dict) -> bool:
    from sil.profile_report import validate_profile_report
    return validate_profile_report(report) == []


def cpu(q: Qualification, runs: Runs) -> None:
    main = runs.manifests["main"]
    main_digest = sha256(main)
    fmu = ["--modules-from", runs.raw["fmu"]]

    # Matching separate debug file, Activation correlation, FMU archive.
    runs.with_debug("matching")
    record = q.scenario("cpu-main-matching-activations", runs.sil(
        "profile", str(main), "--out", "prof", "--collector", "cpu",
        "--activations", *fmu, *DEADLINE))
    report = _profile_checks(q, runs, record, "main", run_status="succeeded",
                             capture="complete", exit_code=0)
    if report:
        _same_outcome(q, runs, record, "main",
                      Path(record["cwd"]) / "prof" / "run.mcap")
        _manifest_identity(q, record, main, main_digest)
        symbols = _self_symbols(report)
        for role, functions in HOT.items():
            for function in functions:
                q.check(record, f"{role} hot function {function} named",
                        symbols.get(function, 0) > 0)
        native = _module(report, "libsilproof_native.so")
        q.check(record, "stripped Native library: matching debug file used",
                native and native["debug"]["status"] == "separate_matching",
                native and native["debug"])
        fmu_module = _module(report, "EgoMotion.so")
        q.check(record, "FMU extracted into the Run directory is named",
                symbols.get("silproof_fmu_spin", 0) > 0 and fmu_module and
                fmu_module["material"] != "unavailable",
                fmu_module and fmu_module.get("material"))
        children = [p for p in report["capture"]["processes"]
                    if any("silproof_child" in c for c in p["commands"])]
        q.check(record, "4 short-lived children, each its own identity "
                "with samples", len(children) == 4 and
                all(p["samples"] > 0 for p in children) and
                len({p["id"] for p in children}) == 4,
                [(p["id"], p["pid"], p["samples"]) for p in children])
        correlation = report["activation_correlation"]
        q.check(record, "correlation complete", correlation.get("status") ==
                "complete", correlation.get("reasons"))
        summary = correlation.get("summary") or {}
        tasks = {t["participant"]: t for t in summary.get("tasks", [])}
        native_task = tasks.get("native", {})
        q.check(record, "Native samples are in their Task's execution "
                "context", native_task.get("execution_context", {})
                .get("samples", 0) > 0)
        q.check(record, "Process samples are window-associated with their "
                "Step", tasks.get("process", {}).get("window_associated", {})
                .get("samples", 0) > 0)
        q.check(record, "every Process mapping verified (one PID namespace)",
                summary.get("processes") and all(
                    p["mapping"] == "verified" for p in summary["processes"]),
                summary.get("processes"))
        _keep_set(q, record)

    for variant, status in (("missing", "not_found"),
                            ("mismatched", "separate_mismatched")):
        runs.with_debug(variant)
        record = q.scenario(f"cpu-main-debug-{variant}", runs.sil(
            "profile", str(main), "--out", "prof", "--collector", "cpu",
            *fmu, *DEADLINE))
        report = _profile_checks(q, runs, record, "main",
                                 run_status="succeeded", capture="complete",
                                 exit_code=0)
        if report:
            native = _module(report, "libsilproof_native.so")
            q.check(record, f"debug file {variant}: status {status}",
                    native and native["debug"]["status"] == status,
                    native and native["debug"])
            symbols = _self_symbols(report)
            q.check(record, "static Native functions have no name",
                    not any(s.startswith("silproof_native_spin")
                            for s in symbols))
            q.check(record, "Native samples stay in the module",
                    native and native["samples_leaf"] > 0)
            q.check(record, "other hot functions still named",
                    symbols.get("silproof_fmu_spin", 0) > 0 and
                    symbols.get("silproof_process_spin", 0) > 0)
            _same_outcome(q, runs, record, "main",
                          Path(record["cwd"]) / "prof" / "run.mcap")
    runs.with_debug("matching")

    # Replay, mixed Periods, FMU group, unshimmed Python probe.
    mixed = runs.manifests["mixed"]
    record = q.scenario("cpu-mixed-activations", runs.sil(
        "profile", str(mixed), "--out", "prof", "--collector", "cpu",
        "--activations", *fmu, "--modules-from", runs.raw["can_node_fmu"],
        "--modules-from", runs.raw["can_bus_fmu"], *DEADLINE))
    report = _profile_checks(q, runs, record, "mixed",
                             run_status="succeeded", capture="complete",
                             exit_code=0)
    if report:
        _same_outcome(q, runs, record, "mixed",
                      Path(record["cwd"]) / "prof" / "run.mcap")
        correlation = report["activation_correlation"]
        q.check(record, "correlation complete",
                correlation.get("status") == "complete",
                correlation.get("reasons"))
        summary = correlation.get("summary") or {}
        tasks = {t["participant"]: t for t in summary.get("tasks", [])}
        for participant, period in fixtures.PERIODS_NS.items():
            expected = -(-1000 * fixtures.MS // period)
            task = tasks.get(participant, {})
            q.check(record, f"{participant}: {expected} completed Steps "
                    f"({period // fixtures.MS} ms Period)",
                    task.get("activations") == expected ==
                    task.get("completed"),
                    {k: task.get(k) for k in ("activations", "completed")})
        q.check(record, "Replay Slots are separate intervals",
                summary.get("intervals", {}).get("replay", 0) > 0,
                summary.get("intervals"))
        q.check(record, "FMU group has window-associated samples",
                tasks.get("group", {}).get("window_associated", {})
                .get("samples", 0) > 0)
        _keep_set(q, record)

    for manifest, code in (("failure", 1), ("deadline", 1)):
        record = q.scenario(f"cpu-{manifest}", runs.sil(
            "profile", str(runs.manifests[manifest]), "--out", "prof",
            "--collector", "cpu", *_deadline_for(manifest)))
        report = _profile_checks(q, runs, record, manifest,
                                 run_status="run_failure",
                                 capture="complete", exit_code=code)
        if report:
            _same_outcome(q, runs, record, manifest,
                          Path(record["cwd"]) / "prof" / "run.mcap")

    long = str(runs.manifests["long"])
    record = q.scenario("cpu-interrupted", runs.sil(
        "profile", long, "--out", "prof", "--collector", "cpu",
        "--activations", *DEADLINE), action=_interrupt(q))
    report = _profile_checks(q, runs, record, "long",
                             run_status="interrupted",
                             capture="interrupted", exit_code=1)
    if report:
        q.check(record, "raw perf.data kept", (Path(record["cwd"]) / "prof" /
                                               "cpu" / "perf.data").is_file())

    record = q.scenario("cpu-collector-killed", runs.sil(
        "profile", long, "--out", "prof", "--collector", "cpu", *DEADLINE),
        action=_kill_descendant(q, "perf record"))
    _profile_checks(q, runs, record, "long", run_status="succeeded",
                    capture="failed", exit_code=1, reasons={"backend_failed"})

    record = q.scenario("cpu-capture-limit", runs.sil(
        "profile", str(main), "--out", "prof", "--collector", "cpu",
        "--max-capture-bytes", "2000000", *fmu, *DEADLINE))
    report = _profile_checks(q, runs, record, "main", run_status="succeeded",
                             capture="partial", exit_code=1,
                             reasons={"capture_limit_reached"})
    if report:
        _same_outcome(q, runs, record, "main",
                      Path(record["cwd"]) / "prof" / "run.mcap")
        _keep_set(q, record)

    record = q.scenario("cpu-activation-limit", runs.sil(
        "profile", str(main), "--out", "prof", "--collector", "cpu",
        "--activations", "--max-activation-bytes", "4096", *fmu, *DEADLINE))
    report = _profile_checks(q, runs, record, "main", run_status="succeeded",
                             capture="complete", exit_code=1)
    if report:
        correlation = report["activation_correlation"]
        q.check(record, "correlation partial, stream truncated",
                correlation.get("status") == "partial" and
                "observations_truncated" in correlation.get("reasons", []),
                [correlation.get("status"), correlation.get("reasons")])
        _same_outcome(q, runs, record, "main",
                      Path(record["cwd"]) / "prof" / "run.mcap")
        _keep_set(q, record)


def heap(q: Qualification, runs: Runs) -> None:
    main = runs.manifests["main"]
    main_digest = sha256(main)
    for variant in ("matching", "mismatched", "missing"):
        runs.with_debug(variant)
        record = q.scenario(f"heap-runner-debug-{variant}", runs.sil(
            "profile", str(main), "--out", "prof", "--collector", "heap",
            "--runner", *DEADLINE))
        report = _profile_checks(q, runs, record, "main",
                                 run_status="succeeded", capture="complete",
                                 exit_code=0)
        if not report:
            continue
        _same_outcome(q, runs, record, "main",
                      Path(record["cwd"]) / "prof" / "run.mcap")
        rows = _heap_rows(report)
        correlation = report["capture"].get("activation_correlation", {})
        q.check(record, "Activation correlation unavailable",
                correlation.get("status") == "unavailable", correlation)
        if variant == "matching":
            _manifest_identity(q, record, main, main_digest)
            for site, (calls, size) in NATIVE_SITES.items():
                row = rows.get(site, {})
                q.check(record, f"{site}: exact calls and bytes",
                        (row.get("allocations"), row.get("allocated_bytes"))
                        == (calls, size),
                        [row.get("allocations"), row.get("allocated_bytes")])
            q.check(record, "no Process participant allocation in the runner",
                    not ({"silproof_process_alloc_site",
                          FMU_SITE[0]} & set(rows)))
            _keep_set(q, record)
        else:
            q.check(record, "stripped static sites have no name",
                    not (set(NATIVE_SITES) & set(rows)))
            q.check(record, "allocation counts kept",
                    report["capture"]["analysis"]["totals"]["allocations"]
                    > 1600)
    runs.with_debug("matching")

    record = q.scenario("heap-participant-fmu", runs.sil(
        "profile", str(main), "--out", "prof", "--collector", "heap",
        "--participant", "fmu", *DEADLINE))
    report = _profile_checks(q, runs, record, "main", run_status="succeeded",
                             capture="complete", exit_code=0)
    if report:
        row = _heap_rows(report).get(FMU_SITE[0], {})
        q.check(record, "FMU site: exact calls and bytes",
                (row.get("allocations"), row.get("allocated_bytes")) ==
                FMU_SITE[1:], [row.get("allocations"),
                               row.get("allocated_bytes")])
        q.check(record, "no Native allocation in the FMU scope",
                not (set(NATIVE_SITES) & set(_heap_rows(report))))
        _same_outcome(q, runs, record, "main",
                      Path(record["cwd"]) / "prof" / "run.mcap")

    mixed = str(runs.manifests["mixed"])
    for participant in ("group", "probe"):
        record = q.scenario(f"heap-mixed-{participant}", runs.sil(
            "profile", mixed, "--out", "prof", "--collector", "heap",
            "--participant", participant, *DEADLINE))
        report = _profile_checks(q, runs, record, "mixed",
                                 run_status="succeeded", capture="complete",
                                 exit_code=0)
        if report:
            _same_outcome(q, runs, record, "mixed",
                          Path(record["cwd"]) / "prof" / "run.mcap")
            if participant == "group":
                traced = report["capture"]["analysis"]["traced_executable"]
                q.check(record, "one scope: the importer of all three FMU "
                        "instances", traced and all(
                            f"--instance {name}" in traced
                            for name in ("node1", "node2", "bus")), traced)
                record["observations"]["modules"] = sorted(
                    m["path"].rsplit("/", 1)[-1] for m in
                    report["capture"]["analysis"]["modules"])
                _keep_set(q, record)

    record = q.scenario("heap-failure", runs.sil(
        "profile", str(runs.manifests["failure"]), "--out", "prof",
        "--collector", "heap", "--runner", *DEADLINE))
    report = _profile_checks(q, runs, record, "failure",
                             run_status="run_failure", capture="complete",
                             exit_code=1)
    if report:
        _same_outcome(q, runs, record, "failure",
                      Path(record["cwd"]) / "prof" / "run.mcap")

    long = str(runs.manifests["long"])
    record = q.scenario("heap-interrupted", runs.sil(
        "profile", long, "--out", "prof", "--collector", "heap", "--runner",
        *DEADLINE), action=_interrupt(q))
    _profile_checks(q, runs, record, "long", run_status="interrupted",
                    capture="interrupted", exit_code=1)

    # The interpreter is outside the Run; the traced process then writes to
    # a FIFO without a reader. What the Run does is the observation.
    record = q.scenario("heap-collector-killed", runs.sil(
        "profile", long, "--out", "prof", "--collector", "heap",
        "--participant", "probe", *DEADLINE),
        action=_kill_descendant(q, "heaptrack_interpret"))
    report = _profile(record)
    _observe(q, record, report)
    q.check(record, "command ends with exit 1 or the Run's failure",
            record["exit_code"] == 1 and not record["timed_out"],
            record["stderr_tail"][-600:])
    if q.check(record, "profile.json written", report is not None):
        q.check(record, "capture not complete",
                report["capture"]["status"] != "complete",
                report["capture"]["reasons"])
        record["observations"]["run_after_kill"] = report["run"]
    q.clean(record, PROFILE_OUTPUTS)

    record = q.scenario("heap-capture-limit", runs.sil(
        "profile", str(main), "--out", "prof", "--collector", "heap",
        "--runner", "--max-capture-bytes", "65536", *DEADLINE))
    report = _profile_checks(q, runs, record, "main", run_status="succeeded",
                             capture="partial", exit_code=1)
    if report:
        _same_outcome(q, runs, record, "main",
                      Path(record["cwd"]) / "prof" / "run.mcap")
        _keep_set(q, record)


STORAGE_RESERVE = 8 << 20  # sil.profile_storage.RESERVE_BYTES


def storage(q: Qualification, runs: Runs, small: Path) -> None:
    """Real storage exhaustion: the profile directory on a small file
    system, the Recording elsewhere. The free space is a little above the
    reserve of the reports, so the capture reaches the reserve while the
    Run goes on."""
    main = str(runs.manifests["main"])
    for collector, free in ((["cpu"], STORAGE_RESERVE + (4 << 20)),
                            (["heap", "--runner"],
                             STORAGE_RESERVE + (64 << 10))):
        name = f"storage-exhausted-{collector[0]}"
        out = small / name
        filler = _fill(small, free)
        record = q.scenario(name, runs.sil(
            "profile", main, "--out", str(out), "--collector", *collector,
            "-o", "run.mcap", *DEADLINE))
        report = (json.loads((out / "profile.json").read_text())
                  if (out / "profile.json").is_file() else None)
        record["observations"] = {
            "wall_ms": record["wall_ms"], "small_bytes": _sizes(out)
            if out.is_dir() else None,
            "profile_written": report is not None,
            "run": report and report["run"], "capture": report and [
                report["capture"]["status"], report["capture"]["reasons"]]}
        q.check(record, "command ends, not exit 0",
                not record["timed_out"] and record["exit_code"] != 0,
                [record["exit_code"], record["stderr_tail"][-600:]])
        q.check(record, "no process left", not record["leftover_processes"],
                record["leftover_processes"])
        q.check(record, "unrelated file unchanged",
                record["sentinel_unchanged"])
        recording = Path(record["cwd"]) / "run.mcap"
        record["observations"]["recording_equals_ordinary"] = (
            recording.is_file() and sha256(recording) ==
            runs.outcomes.get("ordinary-main", {}).get("recording"))
        record["observations"]["exit_code"] = record["exit_code"]
        q.check(record, "the Run goes on: Recording equals the ordinary "
                "Run", record["observations"]["recording_equals_ordinary"])
        q.check(record, "profile.json keeps the Run outcome",
                report is not None and report["run"]["status"] == "succeeded",
                report and report["run"])
        if report is not None:
            q.check(record, "capture not complete, reserve reached",
                    report["capture"]["status"] != "complete" and
                    "storage_reserve_reached" in report["capture"]["reasons"],
                    report["capture"]["reasons"])
        shutil.rmtree(out, ignore_errors=True)
        filler.unlink()


def _fill(small: Path, free: int) -> Path:
    """Leave about `free` bytes on the small file system."""
    stat = os.statvfs(small)
    size = max(0, stat.f_bavail * stat.f_frsize - free)
    filler = small / "filler"
    with filler.open("wb") as stream:
        block = b"\0" * (1 << 20)
        while size > 0:
            size -= stream.write(block[:min(size, len(block))])
    return filler


def run(work: Path, evidence: Path, environment: str, perf: str,
        groups: list[str], small: Path | None) -> int:
    q = Qualification(work, evidence, environment, perf)
    facts = environment_facts()
    runs = Runs(q)
    selected = groups or [g for g in GROUPS if g != "storage"]
    # The references of every profiled Run come first.
    if {"cpu", "heap", "storage"} & set(selected) and \
            "equivalence" not in selected:
        selected.insert(0, "equivalence")
    for group in GROUPS:
        if group not in selected:
            continue
        if group == "equivalence":
            equivalence(q, runs)
        elif group == "preflight":
            preflight(q, runs)
        elif group == "cpu":
            cpu(q, runs)
        elif group == "heap":
            heap(q, runs)
        elif group == "storage":
            storage(q, runs, small)
    q.write(facts, {"groups": selected})
    print(f"{environment}: {len(q.scenarios)} scenarios, "
          f"{q.failed} failed checks")
    return 1 if q.failed else 0


def render(sets: Path, evidence: Path) -> int:
    """Render each copied set offline, as a user on another host does."""
    q = Qualification(evidence.parent / f"{evidence.name}-work", evidence,
                      "offline-render", "none")
    facts = environment_facts()
    for directory in sorted(p for p in sets.iterdir() if p.is_dir()):
        html = q.work / f"{directory.name}.html"
        record = q.scenario(f"render-{directory.name}", [
            "sil", "profile", "render", str(directory / "profile.json"),
            "--html", str(html)])
        q.check(record, "exit 0", record["exit_code"] == 0,
                record["stderr_tail"][-400:])
        q.check(record, "HTML written", html.is_file())
        page = html.read_text() if html.is_file() else ""
        q.check(record, "no network reference in the page",
                not any(s in page for s in ('src="http', "src='http",
                                            'href="http', "url(http")))
        q.check(record, "summary printed", "capture:" in record["stdout_tail"])
        record["observations"] = {
            "wall_ms": record["wall_ms"],
            "set_bytes": _sizes(directory), "html_bytes": len(page.encode())}
        q.clean(record, set())
    q.write(facts)
    print(f"render: {len(q.scenarios)} sets, {q.failed} failed checks")
    return 1 if q.failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("work", type=Path)
    r = sub.add_parser("run")
    r.add_argument("work", type=Path)
    r.add_argument("evidence", type=Path)
    r.add_argument("--environment", required=True)
    r.add_argument("--perf", choices=("available", "denied"), required=True)
    r.add_argument("--group", action="append", choices=GROUPS, default=[])
    r.add_argument("--small-dir", type=Path)
    x = sub.add_parser("render")
    x.add_argument("sets", type=Path)
    x.add_argument("evidence", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        fixtures.build(args.work.resolve() / "workload")
        return 0
    if args.command == "run":
        if "storage" in args.group and args.small_dir is None:
            parser.error("--group storage needs --small-dir")
        return run(args.work.resolve(), args.evidence.resolve(),
                   args.environment, args.perf, list(args.group),
                   args.small_dir and args.small_dir.resolve())
    return render(args.sets.resolve(), args.evidence.resolve())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
