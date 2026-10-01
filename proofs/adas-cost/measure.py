"""Measure the ADAS reference workload in three execution forms (issue #229).

With installed SiL on PATH (`sil-run`, `sil-run-instrumented`, `silschema`,
the `sil` wheel for `python3`, and `include/sil` under the prefix) and a C
compiler:

1. **Artifacts.** Build the library (Native participant and C API in one
   file), `AdasReference.fmu` and the application timing harness from the
   same sources and flags. Record each digest and the compiler.
2. **Inputs.** Generate the declared maneuver per Run length and convert one
   input Recording per instance (workload.py).
3. **Forms.** Author the Manifest of each form from one declaration and
   require that only the controller entries differ.
4. **Counters.** Run every row twice under `sil-run-instrumented`. Its
   copy, route and replay counters must repeat exactly.
5. **Timings.** Run every row `--warmup` times untimed, then `--repeats`
   times under the production `sil-run`, timed with wall-clock, CPU and
   peak RSS of the Run's process tree.
6. **Inertness.** Every production Recording must equal the instrumented
   Recording byte for byte, and Recording on or off must change no counter
   but `recorded`.
7. **Equivalence.** Every form must publish byte-identical Commands.
8. **Application.** Time the application's own calls over the same inputs
   in-process (app_cost.c) and require that it computes the published
   Commands.
9. **Estimates.** Derive startup, per-activation, adaptation/routing and
   Recording costs from the medians. They are labelled estimates.

    python measure.py OUT_DIR [--long-s 60] [--warmup 1] [--repeats 5]

writes `results.json` and `results.md` into OUT_DIR, every artifact, input
and Manifest into OUT_DIR/work, and exits 1 when a check fails. Timings are
observational; counters and checks are deterministic.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import report
import workload
from workload import EXAMPLE_DIR, FORMS, PROOF_DIR, Row

from sil.recording import read_records
from sil.schema import MessageType

package = workload.load("adas_cost_package", EXAMPLE_DIR / "fmu" / "package.py")
adapter = workload.load("adas_cost_adapter", EXAMPLE_DIR / "process_adapter.py")

COMMAND = MessageType("adas.Command", workload.manifest.SCHEMAS["adas.Command"])
# A Process participant that stops answering fails the Run instead of
# stopping it. The deadline is not Manifest data: it changes no Recording.
PARTICIPANT_TIMEOUT_MS = 30_000
FLAGS = ("-std=c11", "-O2", "-ffp-contract=off", "-Wall", "-Wextra",
         "-shared", "-fPIC")


class MeasurementError(RuntimeError):
    """A tool, a build or a Run the measurement depends on failed."""


def _run(args: list[str], **kwargs) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        raise MeasurementError(f"{' '.join(args)} exited {proc.returncode}: "
                               f"{proc.stderr.strip()}")
    return proc.stdout


def _tool(name: str) -> Path:
    path = shutil.which(name)
    if path is None:
        raise MeasurementError(f"no {name} on PATH; install SiL first")
    return Path(path)


def _first_line(args: list[str]) -> str:
    return _run(args).strip().splitlines()[0]


# --- one Run ---------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """What one Run cost, as the operating system reports it."""

    exit_code: int
    wall_s: float
    user_s: float
    system_s: float
    max_rss_bytes: int


def _max_rss_bytes(usage) -> int:
    """ru_maxrss is bytes on macOS and kilobytes on Linux; report bytes."""
    return usage.ru_maxrss if sys.platform == "darwin" else \
        usage.ru_maxrss * 1024


def run_once(args: list[str], cwd: Path, log: Path,
             env: dict | None = None) -> Sample:
    """Runs `sil-run` to completion. wait4 attributes CPU and peak RSS to
    this Run's process tree alone: peak RSS is that of its largest process.
    stderr goes to `log`, stdout (the Manifest hash) is discarded."""
    with log.open("w") as err:
        start = time.perf_counter()
        proc = subprocess.Popen(args, cwd=cwd, env=env,
                                stdout=subprocess.DEVNULL, stderr=err)
        _, status, usage = os.wait4(proc.pid, 0)
        wall = time.perf_counter() - start
    proc.returncode = os.waitstatus_to_exitcode(status)
    return Sample(proc.returncode, wall, usage.ru_utime, usage.ru_stime,
                  _max_rss_bytes(usage))


def _runner_args(runner: Path, manifest: Path, recording: Path | None):
    output = ["-o", str(recording)] if recording else ["--no-recording"]
    return [str(runner), str(manifest), *output,
            "--participant-timeout-ms", str(PARTICIPANT_TIMEOUT_MS)]


def _summary(values: list[float]) -> dict:
    return {"median": statistics.median(values), "min": min(values),
            "max": max(values), "samples": values}


# --- the measurement ---------------------------------------------------------------


class Measurement:
    def __init__(self, out: Path, cc: str, long_s: int, warmup: int,
                 repeats: int):
        self.out = out
        self.work = out / "work"
        self.cc = cc
        self.long_s = long_s
        self.warmup = warmup
        self.repeats = repeats
        self.runner = _tool("sil-run")
        self.instrumented = _tool("sil-run-instrumented")
        self.prefix = self.runner.resolve().parents[1]
        self.python = str(_tool("python3"))
        self.checks: dict[str, dict] = {}

    def check(self, name: str, passed: bool, detail) -> None:
        self.checks[name] = {"passed": bool(passed), "detail": detail}

    # --- artifacts and inputs ---------------------------------------------------

    def artifacts(self) -> dict:
        self.work.mkdir(parents=True, exist_ok=True)
        generated = self.work / "include"
        _run(["silschema", str(EXAMPLE_DIR / "schemas.json"),
              str(generated / "adas_messages.h")])
        self.library = self.work / "adas_reference.so"
        _run([self.cc, *FLAGS, f"-I{self.prefix / 'include'}",
              f"-I{EXAMPLE_DIR}", f"-I{generated}", "-o", str(self.library),
              str(EXAMPLE_DIR / "adas_reference.c"),
              str(EXAMPLE_DIR / "sil_adapter.c"), "-lm"])
        self.harness = self.work / "app_cost.so"
        _run([self.cc, *FLAGS, f"-I{EXAMPLE_DIR}", "-o", str(self.harness),
              str(PROOF_DIR / "app_cost.c"),
              str(EXAMPLE_DIR / "adas_reference.c"), "-lm"])
        identity = package.build(self.work / "fmu", cc=self.cc)
        self.fmu = self.work / "fmu" / f"{package.MODEL_IDENTIFIER}.fmu"
        return {
            "compiler": _first_line([self.cc, "--version"]),
            "flags": list(FLAGS),
            "library": {"sha256": workload.sha256(self.library),
                        "sources": ["adas_reference.c", "sil_adapter.c"]},
            "process_adapter": {
                "sha256": workload.sha256(EXAMPLE_DIR / "process_adapter.py")},
            "fmu": {"sha256": identity["archive_sha256"],
                    "instantiation_token": identity["instantiation_token"]},
            "harness": {"sha256": workload.sha256(self.harness),
                        "sources": ["app_cost.c", "adas_reference.c"]},
        }

    def inputs(self) -> dict:
        most = max(workload.INSTANCES)
        return {w.name: workload.write_inputs(w, most, self._inputs(w))
                for w in workload.workloads(self.long_s).values()}

    def _inputs(self, w: workload.Workload) -> Path:
        return self.work / "inputs" / w.name

    def manifests(self, rows: list[Row]) -> dict:
        # Both Process forms start `python3` from PATH, as an adopter's
        # Manifest would; machine() records which interpreter that is.
        artifacts = workload.Artifacts(self.library, self.fmu)
        self.paths: dict[str, Path] = {}
        hashes = {}
        docs: dict[tuple, dict[str, dict]] = {}
        (self.work / "manifests").mkdir(exist_ok=True)
        for row in rows:
            m = workload.row_manifest(row, self._inputs(row.workload),
                                      artifacts)
            ref = m.write(self.work / "manifests" / f"{row.name}.json")
            self.paths[row.name] = ref.path
            hashes[row.name] = ref.hash
            key = (row.workload.name, row.instances, row.recording)
            docs.setdefault(key, {})[row.form] = m.to_doc()
        failures = []
        for (name, instances, _), forms in docs.items():
            try:
                workload.only_the_controller_differs(forms, instances)
            except workload.FormError as error:
                failures.append(f"{name} x{instances}: {error}")
        self.check("only_the_controller_differs", not failures, failures)
        return hashes

    # --- rows -----------------------------------------------------------------------

    def row(self, row: Row) -> dict:
        directory = self.work / "runs" / row.name
        directory.mkdir(parents=True, exist_ok=True)
        manifest = self.paths[row.name]

        def recording(label: str) -> Path | None:
            return directory / f"{label}.mcap" if row.recording else None

        reports = []
        for i in (1, 2):
            report = directory / f"counters-{i}.json"
            env = dict(os.environ, SIL_COPY_COUNTERS_OUT=str(report))
            sample = run_once(_runner_args(self.instrumented, manifest,
                                           recording(f"instrumented-{i}")),
                              directory, directory / f"instrumented-{i}.log",
                              env)
            reports.append({"exit": sample.exit_code,
                            **json.loads(report.read_text())})
        for i in range(self.warmup):
            run_once(_runner_args(self.runner, manifest,
                                  recording(f"warmup-{i}")),
                     directory, directory / f"warmup-{i}.log")
        samples, identical = [], []
        for i in range(self.repeats):
            out = recording(f"timed-{i}")
            samples.append(run_once(_runner_args(self.runner, manifest, out),
                                    directory, directory / f"timed-{i}.log"))
            if out is not None:
                identical.append(out.read_bytes() == recording(
                    "instrumented-1").read_bytes())
                if i:
                    out.unlink()
        for path in directory.glob("warmup-*.mcap"):
            path.unlink()
        return self._row_result(row, reports, samples, identical)

    def _row_result(self, row: Row, reports: list[dict],
                    samples: list[Sample], identical: list[bool]) -> dict:
        exits = [r["exit"] for r in reports] + [s.exit_code for s in samples]
        wall = _summary([s.wall_s for s in samples])
        return {
            "name": row.name, "form": row.form,
            "workload": row.workload.name, "instances": row.instances,
            "recording": row.recording,
            "activations": row.workload.activations,
            "simulated_s": row.workload.duration_ns / 1e9,
            "deterministic": {
                "exit_codes": exits,
                "counters": reports[0]["deterministic"],
                "counters_repeat": reports[0]["deterministic"]
                == reports[1]["deterministic"],
                "recording_equals_instrumented": identical,
            },
            "observational": {
                "wall_s": wall,
                "user_s": _summary([s.user_s for s in samples]),
                "system_s": _summary([s.system_s for s in samples]),
                "tree_max_rss_bytes": max(s.max_rss_bytes for s in samples),
                "real_time_factor": row.workload.duration_ns / 1e9
                / wall["median"],
                # One instrumented Run: the kernel process alone, carrying
                # the counters' own small overhead.
                "instrumented_kernel": reports[0]["observational"],
            },
        }

    # --- deterministic checks -------------------------------------------------------

    def inertness(self, results: list[dict]) -> None:
        exits = {r["name"]: r["deterministic"]["exit_codes"] for r in results
                 if any(r["deterministic"]["exit_codes"])}
        self.check("every_run_exits_0", not exits, exits)
        unrepeated = [r["name"] for r in results
                      if not r["deterministic"]["counters_repeat"]]
        self.check("counters_repeat", not unrepeated, unrepeated)
        differing = [r["name"] for r in results
                     if not all(r["deterministic"][
                         "recording_equals_instrumented"])]
        self.check("recording_equals_instrumented", not differing, differing)
        by_name = {r["name"]: r for r in results}
        changed = []
        for r in results:
            if r["recording"]:
                continue
            on = by_name[r["name"].replace("-recoff", "-recon")]
            off_counters = dict(r["deterministic"]["counters"],
                                recorded=None)
            on_counters = dict(on["deterministic"]["counters"],
                               recorded=None)
            if off_counters != on_counters:
                changed.append(r["name"])
        self.check("recording_changes_only_recorded", not changed, changed)

    def _commands(self, row_name: str) -> list[tuple[str, int, bytes]]:
        path = self.work / "runs" / row_name / "instrumented-1.mcap"
        return [(c, t, d) for c, t, d in read_records(path)
                if c.endswith(".command")]

    def equivalence(self, rows: list[Row]) -> dict:
        report, failures = {}, []
        for row in rows:
            if row.form != "native" or not row.recording:
                continue
            native = self._commands(row.name)
            expected = row.workload.activations * row.instances
            modes: dict[int, int] = {}
            for _, _, data in native:
                mode = COMMAND.unpack(data)["mode"]
                modes[mode] = modes.get(mode, 0) + 1
            entry = {"commands": len(native), "modes": modes,
                     "forms_equal": {}}
            if len(native) != expected:
                failures.append(f"{row.name}: {len(native)} Commands, "
                                f"expected {expected}")
            for form in FORMS[1:]:
                other = row.name.replace("native-", f"{form}-", 1)
                equal = self._commands(other) == native
                entry["forms_equal"][form] = equal
                if not equal:
                    failures.append(f"{other} differs from {row.name}")
            report[row.name.removeprefix("native-")] = entry
        self.check("forms_publish_identical_commands", not failures, failures)
        return report

    # --- the application alone ---------------------------------------------------------

    def application(self) -> dict:
        harness = ctypes.CDLL(str(self.harness))
        harness.adas_cost_run.argtypes = [
            ctypes.POINTER(adapter.Config), ctypes.POINTER(Activation),
            ctypes.c_size_t, ctypes.POINTER(adapter.Output),
            ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(adapter.Fault)]
        harness.adas_cost_run.restype = ctypes.c_int
        config = adapter.Config(workload.PERIOD_NS,
                                *workload.manifest.PARAMETERS.values())
        report, failures = {}, []
        for w in workload.workloads(self.long_s).values():
            activations = read_activations(
                self._inputs(w) / "load0.inputs.csv")
            n = len(activations)
            outputs = (adapter.Output * n)()
            fault = adapter.Fault()
            elapsed = ctypes.c_uint64()
            per_activation = []
            for i in range(self.warmup + self.repeats):
                status = harness.adas_cost_run(config, activations, n,
                                               outputs, ctypes.byref(elapsed),
                                               fault)
                if status != adapter.OK:
                    raise MeasurementError(
                        f"app_cost {w.name}: {fault.message.decode()}")
                if i >= self.warmup:
                    per_activation.append(elapsed.value / n / 1e3)
            published = [d for c, _, d in self._commands(
                f"native-{w.name}-x1-recon") if c == "load0.command"]
            computed = [pack_output(o) for o in outputs]
            if computed != published:
                failures.append(f"{w.name}: the harness computed other "
                                "Commands than the Runs published")
            report[w.name] = {"activations": n,
                              "us_per_activation": _summary(per_activation)}
        self.check("application_computes_the_published_commands",
                   not failures, failures)
        return report


# --- the timing harness's inputs ------------------------------------------------------


class Activation(ctypes.Structure):
    """`adas_cost_activation` from app_cost.c, field for field."""

    _fields_ = [("t_ns", ctypes.c_uint64), ("has_radar", ctypes.c_uint32),
                ("has_camera", ctypes.c_uint32), ("has_ego", ctypes.c_uint32),
                ("radar", adapter.ObjectList), ("camera", adapter.ObjectList),
                ("ego", adapter.Ego)]


def _object_list(row: dict, sensor: str) -> adapter.ObjectList:
    count = int(row[f"{sensor}_count"])
    objects = [adapter.Object(
        int(row[f"{sensor}_object_id_{i}"]),
        *(float(row[f"{sensor}_{field}_{i}"])
          for field in adapter.OBJECT_FIELDS[1:])) for i in range(count)]
    return adapter.ObjectList(
        int(row[f"{sensor}_t_ns"]), int(row[f"{sensor}_sensor_id"]),
        int(row[f"{sensor}_frame_id"]), int(row[f"{sensor}_sequence"]),
        count, int(row[f"{sensor}_validity"]),
        (adapter.Object * adapter.MAX_OBJECTS)(*objects))


def read_activations(expanded: Path):
    """The activations of an expanded maneuver (prepare.py), as the harness
    takes them: every row is one activation at its `time_ms`."""
    with expanded.open(newline="") as f:
        rows = list(csv.DictReader(f))
    activations = (Activation * len(rows))()
    for a, row in zip(activations, rows):
        a.t_ns = int(row["time_ms"]) * workload.MS
        for sensor in ("radar", "camera"):
            if row[f"{sensor}_t_ns"]:
                setattr(a, f"has_{sensor}", 1)
                setattr(a, sensor, _object_list(row, sensor))
        if row["ego_t_ns"]:
            a.has_ego = 1
            a.ego = adapter.Ego(int(row["ego_t_ns"]), int(row["ego_sequence"]),
                                int(row["ego_validity"]),
                                float(row["ego_speed_mps"]))
    return activations


def pack_output(o) -> bytes:
    return COMMAND.pack(**{name: getattr(o, name)
                           for name in COMMAND.field_names})


# --- the estimates ------------------------------------------------------------------------


def estimates(results: list[dict], application: dict, long_s: int) -> dict:
    """Costs derived from medians of the Recording-off rows unless stated.
    Each is an estimate: a difference of observations, not a measurement of
    one mechanism. A difference is `resolved` only when it exceeds the
    summed spread (max - min) of the two rows it comes from; otherwise it is
    within the noise of this machine and policy."""
    w = workload.workloads(long_s)
    wall = {(r["form"], r["workload"], r["instances"], r["recording"]):
            r["observational"]["wall_s"] for r in results}
    app_us = application["long"]["us_per_activation"]["median"]
    span = w["long"].activations - w["ci"].activations

    def difference(a: tuple, b: tuple, per: float) -> dict:
        value = wall[a]["median"] - wall[b]["median"]
        spread = sum(wall[k]["max"] - wall[k]["min"] for k in (a, b))
        return {"us": value / per * 1e6, "resolved": abs(value) > spread}

    report = {}
    for form in FORMS:
        for n in workload.INSTANCES:
            step = difference((form, "long", n, False), (form, "ci", n, False),
                              span * n)
            report[f"{form}-x{n}"] = {
                "startup_s": wall[(form, "startup", n, False)]["median"],
                "participant_step": step,
                "application_us_per_step": app_us,
                "adaptation_and_routing_us_per_step": step["us"] - app_us,
                "recording_per_participant_step": difference(
                    (form, "long", n, True), (form, "long", n, False),
                    w["long"].activations * n),
            }
    for n in workload.INSTANCES:
        report[f"fmu-x{n}"]["startup_over_process_s"] = (
            report[f"fmu-x{n}"]["startup_s"]
            - report[f"process-x{n}"]["startup_s"])
    return report


# --- the environment ---------------------------------------------------------------------------


def _cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    if sys.platform == "darwin":
        return _first_line(["sysctl", "-n", "machdep.cpu.brand_string"])
    return platform.processor() or "unknown"


def _memory_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return None


def machine(m: Measurement) -> dict:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": _cpu_model(),
        "logical_cpus": os.cpu_count(),
        "memory_bytes": _memory_bytes(),
        "participant_python": _first_line(
            [m.python, "-c", "import sys; print(sys.version)"]),
        "driver_python": sys.version.split()[0],
        "sil_run": _first_line([str(m.runner), "--version"]),
        "runners": {"production": str(m.runner),
                    "instrumented": str(m.instrumented)},
    }


# --- driver -------------------------------------------------------------------------------------


def measure(out: Path, cc: str = "cc", long_s: int = workload.LONG_S,
            warmup: int = workload.WARMUP,
            repeats: int = workload.REPEATS) -> dict:
    m = Measurement(out, cc, long_s, warmup, repeats)
    rows = workload.matrix(long_s)
    result = {"declaration": workload.declaration(long_s, warmup, repeats),
              "machine": machine(m), "artifacts": m.artifacts(),
              "inputs": m.inputs()}
    result["manifest_hashes"] = m.manifests(rows)
    results = []
    for index, row in enumerate(rows, 1):
        print(f"[{index}/{len(rows)}] {row.name}", file=sys.stderr,
              flush=True)
        results.append(m.row(row))
    result["rows"] = results
    m.inertness(results)
    result["equivalence"] = m.equivalence(rows)
    result["application"] = m.application()
    result["estimates"] = estimates(results, result["application"], long_s)
    result["checks"] = m.checks
    result["passed"] = all(c["passed"] for c in m.checks.values())
    (out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    (out / "results.md").write_text(report.render(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="the evidence directory")
    parser.add_argument("--cc", default="cc", help="the C compiler")
    parser.add_argument("--long-s", type=int, default=workload.LONG_S,
                        help="simulated seconds of the long Run")
    parser.add_argument("--warmup", type=int, default=workload.WARMUP,
                        help="untimed Runs per row before the timed ones")
    parser.add_argument("--repeats", type=int, default=workload.REPEATS,
                        help="timed Runs per row")
    args = parser.parse_args()
    if args.repeats < 1 or args.warmup < 0 or args.long_s < 1:
        parser.error("--repeats and --long-s must be at least 1, "
                     "--warmup at least 0")
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        result = measure(args.out.resolve(), args.cc, args.long_s,
                         args.warmup, args.repeats)
    except MeasurementError as error:
        raise SystemExit(f"measure.py: {error}") from error
    failed = [name for name, c in result["checks"].items() if not c["passed"]]
    print(f"measure.py: {'checks failed: ' + ', '.join(failed) if failed else 'every check passed'}",
          file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
