"""Qualify opendbc's safety library as a Native participant, with transmit (#232).

Runs inside the example image after `acceptance.py`, with no network and no
source tree. The library, the recording and the receive reference come from
the #178 bundle; the frames, the transmit candidates and the Native
reference from the tool image's export; the adapter builds from the example
image (`/opt/libsafety/native/`). Every check raises, so a failed acceptance
cannot write a passing report.

Usage: native_acceptance.py <bundle> <prepared> <workspace>

It writes into the workspace:

- `inputs/native-*`, `inputs/transmit*`: the timed Recordings, windows,
  reference and contract;
- `runs/native-*`: the directly run Manifests, Recordings and runner output;
- `bundles/`: the sealed regression bundles, `matrix/`: the `sil bundle matrix`
  evidence;
- `evidence/native-report.json`.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import shutil
import statistics
import struct
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import manifest
import workload
from acceptance import (
    RECORDING,
    REFERENCE,
    Setup,
    command,
    compare,
    config_valid_when_warm,
    convert,
    pinned_inputs,
    read_json,
    require,
    runtime_identity,
    segment,
    sha256,
    write_json,
    write_reference_csv,
)

from sil.recording import read_records

HERE = Path(__file__).resolve().parent
ADAPTERS = HERE / "native"
ADAPTER = ADAPTERS / "libsafety_native.so"
LIBRARY_COST = ADAPTERS / "library_cost"
FAILURE_ADAPTERS = {"crash": ADAPTERS / "libsafety_native_crash.so",
                    "hang": ADAPTERS / "libsafety_native_hang.so"}
# Built into the failure adapters (Dockerfile): the event the failure starts at.
FAILURE_EVENT = 100
# The crash control's runner ends with SIGSEGV.
CRASH_EXIT = -11
SIMULATED = ("the failure build crashes or hangs in the adapter, just before "
             "the library call; the library itself is unchanged")
# Whole-case guards: a nominal Run takes seconds; the hang must hit its guard.
NOMINAL_TIMEOUT_S = 600
HANG_TIMEOUT_S = 30
COMPILERS = ["cc", "gcc", "clang", "c++"]
# The computation timing: untimed repeats first, then the timed ones, each a
# fresh process because the library's state is C globals.
COST_WARMUP = 1
COST_REPEATS = 5
FRAME_RECORD, CANDIDATE_RECORD = b"\x00", b"\x01"


# Inputs --------------------------------------------------------------------------

def transmit_identity(prepared: Path, pinned: dict) -> dict:
    """The transmit export against the pins and its own digests."""
    pins = pinned["pins"]
    identity = read_json(prepared / "transmit.json")
    for key, pin in (("library", pins["library"]["sha256"]),
                     ("recording", pins["recording"]["sha256"]),
                     ("reference", pins["reference"]["sha256"])):
        require(identity[key]["sha256"] == pin,
                f"the transmit export used another {key}: {identity[key]}")
    require(sha256(prepared / "transmit.csv") == identity["transmit_csv_sha256"],
            "transmit.csv is not the exported file")
    require(sha256(prepared / "native-reference.json")
            == identity["native_reference_sha256"],
            "native-reference.json is not the exported file")
    require(identity["agrees_with_upstream"],
            f"the transmit reference disagrees with upstream replay_drive: "
            f"{identity['upstream_replay_drive']}")
    require(identity["receive_state_divergence_from_178"] is None,
            "the transmit candidates change the receive-side state")
    return identity


def first_candidate_ns(prepared: Path) -> int:
    with (prepared / "transmit.csv").open(newline="") as rows:
        return int(next(csv.DictReader(rows))["log_mono_ns"])


def prepare_inputs(prepared: Path, inputs: Path, evidence: Path, pinned: dict,
                   transmit: dict) -> tuple[dict, list[int]]:
    """The timed frames, the candidates, their windows, the reference and the
    contract, and the observation Slots."""
    frames = pinned["frames"]
    first, last = frames["first_log_mono_ns"], frames["last_log_mono_ns"]
    receipts = {
        "frames": convert("csv", workload.TIMED_FRAME_MAPPING,
                          prepared / "frames.csv", inputs / "native-frames.mcap",
                          evidence),
        "transmit": convert("csv", workload.TRANSMIT_MAPPING,
                            prepared / "transmit.csv", inputs / "transmit.mcap",
                            evidence),
    }
    receipts["frames_window"] = convert(
        "window", workload.timed_window_document(
            first, last, workload.FRAME_CHANNEL, workload.MAX_GAP_NS),
        inputs / "native-frames.mcap", inputs / "native-window.mcap", evidence)
    receipts["transmit_window"] = convert(
        "window", workload.timed_window_document(
            first, last, workload.TRANSMIT_CHANNEL, workload.TRANSMIT_MAX_GAP_NS,
            channel_start_ns=first_candidate_ns(prepared)),
        inputs / "transmit.mcap", inputs / "transmit-window.mcap", evidence)
    for key, channel, count in (
            ("frames_window", workload.FRAME_CHANNEL, frames["received_frames"]),
            ("transmit_window", workload.TRANSMIT_CHANNEL,
             transmit["candidates"]["frames"])):
        coverage = receipts[key]["channels"][channel]
        require(coverage["evaluation"]["messages"] == count
                and coverage["warm_up"]["messages"] == 0,
                f"the {key} does not evaluate every Message: {coverage}")

    trace = read_json(prepared / "native-reference.json")["trace"]
    require(len(trace) == frames["can_events"],
            "the Native reference does not hold one state per event")
    rows = workload.native_reference_rows(trace, first)
    write_reference_csv(rows, inputs / "native-reference.csv")
    receipts["reference"] = convert(
        "csv", workload.NATIVE_REFERENCE_MAPPING,
        inputs / "native-reference.csv", inputs / "native-reference.mcap",
        evidence)
    slots = [row["slot_ns"] for row in rows]
    write_json(inputs / "native-contract.json",
               workload.comparison_contract(slots, workload.NATIVE_STATE_FIELDS))
    return receipts, slots


# Direct Runs ---------------------------------------------------------------------

class Native:
    """What every Native Run of the acceptance shares."""

    def __init__(self, library: Path, seg: manifest.Segment, inputs: Path,
                 runs: Path, evidence: Path, slots: list[int]):
        self.library = library
        self.segment = seg
        self.inputs = inputs
        self.runs = runs
        self.slots = slots
        # acceptance.compare and config_valid_when_warm read these fields.
        self.setup = Setup(library=library, segment=seg,
                           window=inputs / "native-window.mcap",
                           reference=inputs / "native-reference.mcap",
                           contract=inputs / "native-contract.json",
                           slots=slots, runs=runs, evidence=evidence)

    def manifest(self, frames: Path, transmit: Path, adapter: Path,
                 library: Path, **controls) -> manifest.Manifest:
        return manifest.native_manifest(frames, transmit, adapter, library,
                                        self.segment, **controls)

    def run(self, name: str, controls: dict | None = None) -> dict:
        m = self.manifest(self.inputs / "native-window.mcap",
                          self.inputs / "transmit-window.mcap", ADAPTER,
                          self.library, **(controls or {}))
        ref = m.write(self.runs / f"native-{name}.json")
        recording = self.runs / f"native-{name}.mcap"
        start = time.perf_counter()
        proc = subprocess.run(["sil-run", str(ref.path), "-o", str(recording)],
                              capture_output=True, text=True,
                              timeout=NOMINAL_TIMEOUT_S)
        wall_s = time.perf_counter() - start
        (self.runs / f"native-{name}.log").write_text(proc.stdout + proc.stderr)
        return {"manifest_hash": ref.hash, "exit_code": proc.returncode,
                "stderr": proc.stderr, "wall_s": wall_s, "recording": recording,
                "sha256": sha256(recording) if proc.returncode == 0 else None}


def nominal(native: Native) -> dict:
    """Two Runs of one Manifest: byte-identical, and equal to the reference."""
    first, second = (native.run(f"nominal-{i}") for i in (1, 2))
    for result in (first, second):
        require(result["exit_code"] == 0,
                f"the Native nominal Run failed:\n{result['stderr']}")
    require(first["sha256"] == second["sha256"],
            "two Runs of the Native nominal Manifest recorded different bytes")
    report = compare(native.setup, "native-nominal", first["recording"])
    counts = report["channels"][workload.STATE_CHANNEL]
    require(report["verdict"] == "pass" and counts["checked"] == len(native.slots),
            f"the Native Run does not match the reference: "
            f"{report['first_divergence'] or report['coverage']}")
    return {"manifest_hash": first["manifest_hash"],
            "recording_sha256": first["sha256"], "byte_identical": True,
            "observations_checked": counts["checked"],
            "config_valid": config_valid_when_warm(
                native.setup, first["recording"], workload.NATIVE_STATE_SCHEMA),
            "provenance": read_json(Path(f"{first['recording']}.provenance.json")),
            "wall_s": [first["wall_s"], second["wall_s"]],
            "recording": first["recording"]}


def comparison_control(native: Native, name: str, expect) -> dict:
    result = native.run(name, manifest.NATIVE_CONTROLS[name])
    require(result["exit_code"] == 0,
            f"native {name} did not complete:\n{result['stderr']}")
    report = compare(native.setup, f"native-{name}", result["recording"])
    first = report["first_divergence"]
    require(report["verdict"] == "fail", f"native {name} passed the comparison")
    expect(first)
    return {"manifest_hash": result["manifest_hash"], "verdict": "fail",
            "divergences": report["divergences"], "first_divergence": first}


def direct_controls(native: Native, pins: dict) -> dict:
    timer_pin = pins["failing_controls"]["timer-in-ns"]

    def timer_in_ns(first):
        require(first["kind"] == "value" and first["field"] == timer_pin["field"]
                and first["observation_ns"] == native.slots[timer_pin["index"]],
                f"native timer-in-ns diverged elsewhere than #178 found: {first}")

    def stock_longitudinal(first):
        # Only the transmit verdicts change: ACC_CONTROL is refused.
        require(first["kind"] == "value"
                and first["field"] in ("tx_accepted", "tx_rejected"),
                f"stock-longitudinal failed for another reason: {first}")

    second = native.run("second-instance",
                        manifest.NATIVE_CONTROLS["second-instance"])
    require(second["exit_code"] == 2
            and "at most one instance" in second["stderr"],
            f"a second instance must be a Manifest error naming the reason; "
            f"exit {second['exit_code']}:\n{second['stderr']}")
    return {
        "timer-in-ns": comparison_control(native, "timer-in-ns", timer_in_ns),
        "stock-longitudinal": comparison_control(
            native, "stock-longitudinal", stock_longitudinal),
        "second-instance": {"manifest_hash": second["manifest_hash"],
                            "exit_code": 2,
                            "diagnostic": second["stderr"].strip().splitlines()[-1]},
    }


# The library's own computation ----------------------------------------------------

def payloads(recording: Path, channel: str) -> list[bytes]:
    return [data for name, _, data in read_records(recording) if name == channel]


def event_ns(payload: bytes) -> int:
    """A can.TimedFrame or libsafety.NativeState begins with its u64 event_ns."""
    return struct.unpack_from("<Q", payload)[0]


def cost_records(frames: list[bytes], transmit: list[bytes]) -> list[bytes]:
    """The harness records in Run order: the frames of an event, then its
    candidates. A candidate without a frame at its instant is refused."""
    candidates = defaultdict(list)
    for payload in transmit:
        candidates[event_ns(payload)].append(payload)
    records, current = [], None
    for payload in frames:
        if current is not None and event_ns(payload) != current:
            records += [CANDIDATE_RECORD + c for c in candidates.pop(current, [])]
        current = event_ns(payload)
        records.append(FRAME_RECORD + payload)
    records += [CANDIDATE_RECORD + c for c in candidates.pop(current, [])]
    require(not candidates, f"{len(candidates)} candidate instants have no event")
    return records


def cost_input(native: Native, path: Path) -> None:
    """Every frame and candidate of the nominal Run, in Run order."""
    path.write_bytes(b"".join(cost_records(
        payloads(native.inputs / "native-window.mcap", workload.FRAME_CHANNEL),
        payloads(native.inputs / "transmit-window.mcap",
                 workload.TRANSMIT_CHANNEL))))


def library_computation(native: Native, nominal_recording: Path,
                        run_wall_s: float) -> dict:
    """The library calls alone, on the nominal Run's inputs, and proof that
    they compute what the Run published."""
    work = native.runs / "library-cost"
    work.mkdir()
    cost_input(native, work / "input.bin")
    expected = b"".join(payloads(nominal_recording, workload.STATE_CHANNEL))
    seg = native.segment
    timings = []
    for repeat in range(COST_WARMUP + COST_REPEATS):
        output = work / f"output-{repeat}.bin"
        proc = subprocess.run(
            [str(LIBRARY_COST), str(native.library), str(work / "input.bin"),
             str(output), str(seg.mode), str(seg.param),
             str(seg.alternative_experience), str(seg.first_log_mono_ns),
             str(manifest.TIMER_UNIT_NS), "0", str(seg.last_event_ns)],
            capture_output=True, text=True, timeout=NOMINAL_TIMEOUT_S)
        require(proc.returncode == 0, f"library_cost failed:\n{proc.stderr}")
        require(output.read_bytes() == expected,
                "library_cost computed other states than the Run published")
        result = json.loads(proc.stdout)
        if repeat >= COST_WARMUP:
            timings.append(result["elapsed_ns"])
    median = statistics.median(timings)
    return {
        "what": "the pinned library's calls alone, as native_adapter.c makes "
                "them, packets built before the timed interval; equal to the "
                "nominal Run's published states at every event",
        "harness_sha256": sha256(LIBRARY_COST),
        "events": result["events"], "frames": result["frames"],
        "candidates": result["candidates"],
        "warmup": COST_WARMUP, "repeats": COST_REPEATS,
        "elapsed_ns": {"median": median, "min": min(timings),
                       "max": max(timings), "all": timings},
        "us_per_event": median / result["events"] / 1000,
        "ns_per_library_frame": median / (result["frames"] + result["candidates"]),
        "share_of_native_run": median / 1e9 / run_wall_s,
    }


# Sealed bundles under sil bundle matrix ---------------------------------------------------

def loader_files(binary: Path) -> list[str]:
    """The shared libraries the loader resolves for `binary`, declared because
    a provenance side-car digests only what a Manifest names."""
    listing = command("ldd", str(binary))
    return sorted({word for line in listing.splitlines()
                   for word in line.split() if word.startswith("/")})


def build_bundle(native: Native, root: Path, name: str, adapter: Path,
                 tmpdir: Path, *, compared: bool) -> Path:
    """One sealed-ready bundle: the Manifest names only bundle files."""
    root.mkdir(parents=True)
    artifacts = {}

    def place(source: Path, role: str, target: str) -> Path:
        artifacts[target] = role
        shutil.copyfile(source, root / target)
        return root / target

    adapter = place(adapter, "target", "libsafety_native.so")
    library = place(native.library, "target", "libsafety.so")
    frames = place(native.inputs / "native-window.mcap", "recording",
                   "native-window.mcap")
    transmit = place(native.inputs / "transmit-window.mcap", "recording",
                     "transmit-window.mcap")
    run = {"name": name, "manifest": f"{name}.json", "determinism": compared}
    if compared:
        place(native.setup.reference, "reference", "native-reference.mcap")
        place(native.setup.contract, "contract", "native-contract.json")
        run["comparisons"] = [{"name": "reference",
                               "contract": "native-contract.json",
                               "reference": "native-reference.mcap"}]
    native.manifest(frames, transmit, adapter, library).write(root / run["manifest"])
    artifacts[run["manifest"]] = "manifest"
    write_json(root / "bundle.json", {
        "sil_bundle": 1, "name": name, "artifacts": artifacts,
        "runtime": {"runner": "sil-run",
                    "environment": {"PATH": "/usr/local/bin:/usr/bin:/bin",
                                    "TMPDIR": str(tmpdir)}},
        "dependencies": {"files": sorted({*loader_files(adapter),
                                          *loader_files(library)})},
        "excluded": {"executables": COMPILERS},
        "runs": [run],
    })
    command("sil", "bundle", "seal", str(root))
    return root


def matrix(cases: list[dict], case_list: Path, out: Path) -> tuple[int, dict]:
    write_json(case_list, {"sil_matrix": 1, "cases": cases})
    proc = subprocess.run(["sil", "bundle", "matrix", str(case_list), "-o", str(out),
                           "--jobs", "1"], capture_output=True, text=True)
    (out.parent / f"{out.name}.log").write_text(proc.stdout + proc.stderr)
    summary = read_json(out / "summary.json")
    return proc.returncode, {c["name"]: c for c in summary["cases"]}


def case(root: Path, timeout_s: int) -> dict:
    return {"name": root.name, "bundle": str(root),
            "expect_lock": sha256(root / "bundle.lock.json"),
            "timeout_s": timeout_s, "required": True}


def sealed_regression(native: Native, workspace: Path) -> dict:
    """The nominal Run as a sealed offline bundle under a whole-case guard,
    and the crash and hang controls, each failing for its own reason."""
    bundles, out = workspace / "bundles", workspace / "matrix"
    tmp = {name: workspace / "tmp" / name for name in ("native-nominal",
                                                       "native-crash",
                                                       "native-hang")}
    for directory in tmp.values():
        directory.mkdir(parents=True)
    nominal_root = build_bundle(native, bundles / "native-nominal",
                                "native-nominal", ADAPTER, tmp["native-nominal"],
                                compared=True)
    failure_roots = {
        kind: build_bundle(native, bundles / f"native-{kind}", f"native-{kind}",
                           adapter, tmp[f"native-{kind}"], compared=False)
        for kind, adapter in FAILURE_ADAPTERS.items()}
    out.mkdir()
    code, cases = matrix([case(nominal_root, NOMINAL_TIMEOUT_S)],
                         out / "nominal.json", out / "nominal")
    entry = cases["native-nominal"]
    require(code == 0 and entry["status"] == "pass",
            f"the sealed Native regression did not pass: exit {code}, {entry}")
    code, cases = matrix(
        [case(failure_roots["crash"], NOMINAL_TIMEOUT_S),
         case(failure_roots["hang"], HANG_TIMEOUT_S)],
        out / "controls.json", out / "controls")
    require(code == 1, f"the control matrix must fail, exit {code}")
    crash, hang = cases["native-crash"], cases["native-hang"]
    require(crash["status"] == "behavioral-failure"
            and [r["exit_code"] for r in crash["runs"]] == [CRASH_EXIT],
            f"the crash control must end the runner with SIGSEGV: {crash}")
    require(hang["status"] == "timeout"
            and f"{HANG_TIMEOUT_S} s guard" in hang.get("reason", ""),
            f"the hang control must hit its whole-case guard: {hang}")
    # A crash or a kill inside the runner runs no destructor; what it leaves
    # in the case evidence is recorded, not cleaned.
    residue = {kind: sorted(str(path.relative_to(out)) for path in
                            (out / "controls" / "cases" / f"native-{kind}").rglob("*")
                            if path.name.startswith(("core", ".sil-run-")))
               for kind in FAILURE_ADAPTERS}
    reached = {}
    for kind in FAILURE_ADAPTERS:
        marker = tmp[f"native-{kind}"] / "libsafety-failure-event"
        reached[kind] = marker.read_text().strip() if marker.is_file() else None
        require(reached[kind] == str(FAILURE_EVENT),
                f"the {kind} control did not reach event {FAILURE_EVENT}: "
                f"{reached[kind]}")
    return {
        "nominal": {"status": entry["status"], "lock_sha256": sha256(
                        nominal_root / "bundle.lock.json"),
                    "timeout_s": NOMINAL_TIMEOUT_S, "runs": entry["runs"]},
        "crash": {"status": crash["status"], "reason": crash.get("reason"),
                  "simulated": SIMULATED,
                  "runs": crash["runs"], "reached_event": reached["crash"],
                  "residue": residue["crash"]},
        "hang": {"status": hang["status"], "reason": hang.get("reason"),
                 "simulated": SIMULATED,
                 "timeout_s": HANG_TIMEOUT_S, "reached_event": reached["hang"],
                 "residue": residue["hang"]},
    }


# Report ---------------------------------------------------------------------------

def resources(native: Native, native_result: dict, process_report: Path,
              frames: dict) -> dict:
    """Observational cost on this runner: whole Runs of both forms, and the
    library's computation alone."""
    wall = min(native_result["wall_s"])
    process = read_json(process_report)["resources"]
    return {
        "note": "observational, from one runner; not an acceptance criterion "
                "and not a representative vECU measurement for #125",
        "native_run_wall_s": native_result["wall_s"],
        "native_events_per_s": frames["can_events"] / wall,
        "native_virtual_to_wall": (frames["last_log_mono_ns"]
                                   - frames["first_log_mono_ns"]) / 1e9 / wall,
        # The resident set of a runner forked from Python includes the
        # driver's, so it is not reported for the in-process form.
        "process_run_wall_s": process["run_wall_s"],
        "library_computation": library_computation(
            native, native_result["recording"], wall),
    }


def main(bundle: str, prepared: str, workspace: str) -> None:
    bundle, prepared, workspace = Path(bundle), Path(prepared), Path(workspace)
    inputs, runs, evidence = (workspace / name
                              for name in ("inputs", "runs", "evidence"))
    for directory in (inputs, runs, evidence):
        directory.mkdir(parents=True, exist_ok=True)
    pinned = pinned_inputs(bundle, prepared)
    transmit = transmit_identity(prepared, pinned)
    library = bundle / "libsafety/libsafety.so"
    receipts, slots = prepare_inputs(prepared, inputs, evidence, pinned, transmit)
    frames = pinned["frames"]
    last_event_ns = frames["last_log_mono_ns"] - frames["first_log_mono_ns"]
    seg = segment(pinned, last_event_ns)
    seg = dataclasses.replace(
        seg, largest_transmit_burst=transmit["candidates"]["largest_burst"])
    native = Native(library, seg, inputs, runs, evidence, slots)
    result = nominal(native)
    report = {
        "claim": "public-artifact acceptance of one shared library as a Native "
                 "participant against its independent reference, receive and "
                 "recorded-derived transmit; not production-vehicle validation",
        "identities": {
            "library": {"path": "libsafety/libsafety.so", "sha256": sha256(library)},
            "adapters": {path.name: sha256(path)
                         for path in (ADAPTER, *FAILURE_ADAPTERS.values())},
            "source_recording": {"path": str(RECORDING),
                                 "sha256": sha256(bundle / RECORDING)},
            "receive_reference": {"path": str(REFERENCE),
                                  "sha256": sha256(bundle / REFERENCE)},
            "calibration": pinned["pins"]["contract"],
            "transmit_export": transmit,
            "conversion": receipts,
            "runtime": runtime_identity(library),
            "compilers_absent": [c for c in COMPILERS if shutil.which(c) is None],
        },
        "run": {
            "participant": "native: native_adapter.c, dlopens libsafety.so",
            "task_period_ns": workload.STEP_PERIOD_NS,
            "latency_ns": {workload.FRAME_CHANNEL: 0,
                           workload.TRANSMIT_CHANNEL: 0,
                           workload.STATE_CHANNEL: 0},
            "route_capacity": {workload.FRAME_CHANNEL: seg.largest_burst,
                               workload.TRANSMIT_CHANNEL: seg.largest_transmit_burst},
            "duration_ns": workload.duration_ns(last_event_ns),
            "event_time": "frame field event_ns, rebased by sil-window",
            "transmit": "upstream sendcan event after the can event at the "
                        "same instant: set_timer, safety_tick when warm, "
                        "safety_tx_hook per candidate",
            "instances": "one per Run: C globals; a second is a Manifest error",
            "termination": "none: no ABI callback and no library call; the "
                           "library ends with the runner process",
        },
        "nominal": {k: v for k, v in result.items()
                    if k not in ("wall_s", "recording")},
        "contract_sha256": sha256(native.setup.contract),
        "controls": direct_controls(native, pinned["pins"]),
        "sealed_regression": sealed_regression(native, workspace),
        "resources": resources(native, result, evidence / "report.json", frames),
    }
    write_json(evidence / "native-report.json", report)
    print(json.dumps({"native_nominal": report["nominal"]["recording_sha256"],
                      "observations": result["observations_checked"],
                      "controls": {k: v.get("first_divergence") or v.get("diagnostic")
                                   for k, v in report["controls"].items()},
                      "sealed": {k: v["status"] for k, v in
                                 report["sealed_regression"].items()}},
                     indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
