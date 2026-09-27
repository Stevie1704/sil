"""Qualify opendbc's safety library against its independent reference (#193).

Runs inside the example image with no network and no source tree: SiL comes
from the installed wheel and runner, the library and recording from the
#178 bundle, the exported frames from `prepare_frames.py`. Every check
raises, so a failed acceptance cannot write a passing report.

Usage: acceptance.py <bundle> <prepared> <workspace>

The workspace receives `inputs/` (converted Recordings, mappings, window,
contract), `runs/` (Manifests, Recordings, provenance, runner output) and
`evidence/` (receipts, comparison reports and `report.json`).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import manifest
import workload
from adapter import EventPolicy

from sil import schema
from sil.recording import read_records

HERE = Path(__file__).resolve().parent
# The #178 handoff as committed: the pins this acceptance consumes.
HANDOFF = HERE / "handoff.json"
LIBRARY = Path("libsafety/libsafety.so")
RECORDING = Path("recordings/rlog.zst")
REFERENCE = Path("references/libsafety-states.json")
# The bundle's own source entries for the two public artifacts.
SOURCES = ("opendbc", "can_segment")
TIMEOUT_MS = int(os.environ.get("SIL_LIBSAFETY_PARTICIPANT_TIMEOUT_MS", "30000"))
# The hang control needs only to show that the deadline is what stops it.
HANG_TIMEOUT_MS = 5000


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    return path


def read_json(path: Path):
    return json.loads(Path(path).read_text())


def command(*arguments) -> str:
    proc = subprocess.run(arguments, capture_output=True, text=True)
    require(proc.returncode == 0, f"{' '.join(arguments)} exited "
            f"{proc.returncode}:\n{proc.stderr}")
    return proc.stdout


# Identities ---------------------------------------------------------------------

def pinned_inputs(bundle: Path, prepared: Path) -> dict:
    """The bundle's library, recording and reference, against the pins."""
    pins = read_json(HANDOFF)["shared_library_193"]
    listed = read_json(bundle / "bundle.json")
    for path, pin in ((LIBRARY, pins["library"]["sha256"]),
                      (RECORDING, pins["recording"]["sha256"]),
                      (REFERENCE, pins["reference"]["sha256"])):
        require(sha256(bundle / path) == pin == listed["digests"][str(path)],
                f"{path} is not the pinned artifact {pin}")
    frames = read_json(prepared / "frames.json")
    require(frames["recording"]["sha256"] == pins["recording"]["sha256"],
            "the frames were exported from another recording")
    require(sha256(prepared / "frames.csv") == frames["frames_csv_sha256"],
            "frames.csv is not the exported file")
    require(frames["contract"] == pins["contract"],
            f"recorded contract {frames['contract']} is not {pins['contract']}")
    require(frames["can_events"] == pins["recording"]["can_events"],
            "the export does not hold every can event")
    require(frames["first_log_mono_ns"] == pins["recording"]["first_log_mono_ns"],
            "the export starts at another event")
    layout = read_json(prepared / "packet-layout.json")
    require(layout["identical"] and layout["compared"] == frames["received_frames"],
            "the binding's CANPacket_t was not checked against upstream")
    return {"pins": pins, "frames": frames, "packet_layout": layout,
            "sources": {name: listed["sources"][name] for name in SOURCES},
            "bundle_tools": listed["tools"],
            "bundle_json_sha256": sha256(bundle / "bundle.json"),
            "tool_image": read_json(prepared / "tool-image.json")}


def runtime_identity(library: Path) -> dict:
    ldd = command("ldd", str(library))
    require("not found" not in ldd, f"the runtime image cannot load it:\n{ldd}")
    return {
        "machine": platform.machine(),
        "libc": " ".join(platform.libc_ver()),
        "python": platform.python_version(),
        "runner": json.loads(command("sil-run", "--build-info")),
        "libubsan1": command("dpkg-query", "-W", "-f", "${Version}", "libubsan1"),
        "library_ldd": [line.split(" (0x")[0].strip() for line in ldd.splitlines()],
        "example_image_id": os.environ.get("SIL_LIBSAFETY_EXAMPLE_IMAGE_ID"),
    }


# Conversion ---------------------------------------------------------------------

def convert(tool: str, document: dict, source: Path, output: Path,
            evidence: Path) -> dict:
    """One sil-csv or sil-window step, with its document and receipt kept."""
    document_path = write_json(output.with_suffix(".json"), document)
    receipt = evidence / f"{output.stem}.receipt.json"
    command(tool, str(document_path), str(source), "-o", str(output),
            "--receipt", str(receipt))
    return read_json(receipt)


def write_reference_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]),
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def prepare_inputs(bundle: Path, prepared: Path, inputs: Path,
                   evidence: Path, pinned: dict) -> tuple[dict, list[int]]:
    """The converted frames, window and reference, and the observation Slots."""
    frames = pinned["frames"]
    converted = convert("sil-csv", workload.FRAME_MAPPING,
                        prepared / "frames.csv", inputs / "frames.mcap", evidence)
    count = converted["channels"][workload.FRAME_CHANNEL]["messages"]
    require(count == frames["received_frames"],
            f"sil-csv converted {count} of {frames['received_frames']} frames")
    window = convert("sil-window",
                     workload.window_document(frames["first_log_mono_ns"],
                                              frames["last_log_mono_ns"]),
                     inputs / "frames.mcap", inputs / "window.mcap", evidence)
    coverage = window["channels"][workload.FRAME_CHANNEL]
    require(coverage["evaluation"]["messages"] == count
            and coverage["warm_up"]["messages"] == 0,
            f"the window does not evaluate every frame: {coverage}")

    states = read_json(bundle / REFERENCE)["trace"]
    require(len(states) == frames["can_events"],
            "the reference does not hold one state per event")
    rows = workload.reference_rows(states, frames["first_log_mono_ns"])
    write_reference_csv(rows, inputs / "reference.csv")
    reference = convert("sil-csv", workload.REFERENCE_MAPPING,
                        inputs / "reference.csv", inputs / "reference.mcap",
                        evidence)
    slots = [row["slot_ns"] for row in rows]
    require(len(set(slots)) == len(slots), "two events share an observation Slot")
    write_json(inputs / "contract.json", workload.comparison_contract(slots))
    receipts = {"frames": converted, "window": window, "reference": reference}
    return receipts, slots


def segment(pinned: dict, last_event_ns: int) -> manifest.Segment:
    contract = pinned["pins"]["contract"]
    return manifest.Segment(
        first_log_mono_ns=pinned["frames"]["first_log_mono_ns"],
        last_event_ns=last_event_ns, mode=contract["mode"],
        param=contract["param"],
        alternative_experience=contract["alternative_experience"],
        largest_burst=pinned["pins"]["recording"]["received_frames_per_event"]["max"])


# Runs ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Setup:
    """What every Run and comparison of the acceptance share."""

    library: Path
    segment: manifest.Segment
    window: Path
    reference: Path
    contract: Path
    slots: list[int]
    runs: Path
    evidence: Path


def run(setup: Setup, name: str, controls: dict | None = None,
        timeout_ms: int = TIMEOUT_MS) -> dict:
    m = manifest.replay_manifest(setup.window, setup.library, setup.segment,
                                 **(controls or {}))
    ref = m.write(setup.runs / f"{name}.json")
    recording = setup.runs / f"{name}.mcap"
    start = time.perf_counter()
    proc = subprocess.run(
        ["sil-run", str(ref.path), "-o", str(recording),
         "--participant-timeout-ms", str(timeout_ms)],
        capture_output=True, text=True)
    wall_s = time.perf_counter() - start
    (setup.runs / f"{name}.log").write_text(proc.stdout + proc.stderr)
    return {"manifest_hash": ref.hash, "exit_code": proc.returncode,
            "stderr": proc.stderr, "wall_s": wall_s, "recording": recording,
            "sha256": sha256(recording) if proc.returncode == 0 else None}


def compare(setup: Setup, name: str, recording: Path) -> dict:
    proc = subprocess.run(["sil-compare", str(setup.contract), str(recording),
                           str(setup.reference), "--json"],
                          capture_output=True, text=True)
    require(proc.returncode in (0, 1), f"sil-compare could not judge {name}: "
            f"{proc.stderr}")
    report = json.loads(proc.stdout)
    write_json(setup.evidence / f"compare-{name}.json", report)
    return report


def config_valid_when_warm(setup: Setup, recording: Path) -> dict:
    """Upstream's check: the receive checks are valid at every ticked event.

    Before the first tick the library has not judged them, so an event there
    is counted, not required."""
    ticks = EventPolicy(timer_origin_ns=0, timer_unit_ns=1, first_event_ns=0,
                        last_event_ns=setup.segment.last_event_ns).ticks
    decode = schema.load(workload.SCHEMAS)[workload.STATE_SCHEMA].unpack
    observed = [decode(data) for channel, _, data in read_records(recording)
                if channel == workload.STATE_CHANNEL]
    warm = [o["config_valid"] for o in observed if ticks(o["event_ns"])]
    cold = [o["config_valid"] for o in observed if not ticks(o["event_ns"])]
    require(warm and all(warm), f"{warm.count(0)} ticked events report an "
            "invalid safety configuration")
    return {"ticked_events": len(warm), "ticked_invalid": 0,
            "unticked_events": len(cold), "unticked_invalid": cold.count(0)}


def nominal(setup: Setup) -> dict:
    """Two Runs of one Manifest: byte-identical, and equal to the reference."""
    first, second = (run(setup, f"nominal-{i}") for i in (1, 2))
    peak_rss_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    for result in (first, second):
        require(result["exit_code"] == 0, f"the nominal Run failed:\n{result['stderr']}")
    require(first["sha256"] == second["sha256"],
            "two Runs of the nominal Manifest recorded different bytes")
    report = compare(setup, "nominal", first["recording"])
    counts = report["channels"][workload.STATE_CHANNEL]
    require(report["verdict"] == "pass" and counts["checked"] == len(setup.slots),
            f"the nominal Run does not match the reference: "
            f"{report['first_divergence'] or report['coverage']}")
    return {"manifest_hash": first["manifest_hash"],
            "recording_sha256": first["sha256"], "byte_identical": True,
            "observations_checked": counts["checked"],
            "config_valid": config_valid_when_warm(setup, first["recording"]),
            "provenance": read_json(Path(f"{first['recording']}.provenance.json")),
            "wall_s": [first["wall_s"], second["wall_s"]],
            "peak_rss_kib": peak_rss_kib}


def comparison_control(setup: Setup, name: str, expect) -> dict:
    result = run(setup, name, manifest.CONTROLS[name])
    require(result["exit_code"] == 0, f"{name} did not complete:\n{result['stderr']}")
    report = compare(setup, name, result["recording"])
    first = report["first_divergence"]
    require(report["verdict"] == "fail", f"{name} passed the comparison")
    expect(report, first)
    return {"manifest_hash": result["manifest_hash"], "verdict": "fail",
            "divergences": report["divergences"], "first_divergence": first}


def run_failure_control(setup: Setup, name: str, reasons: tuple[str, ...],
                        timeout_ms: int = TIMEOUT_MS) -> dict:
    result = run(setup, name, manifest.CONTROLS[name], timeout_ms)
    require(result["exit_code"] == 1
            and all(reason in result["stderr"] for reason in reasons),
            f"{name} must be a Run failure naming {reasons}; exit "
            f"{result['exit_code']}:\n{result['stderr']}")
    return {"manifest_hash": result["manifest_hash"], "exit_code": 1,
            "diagnostic": result["stderr"].strip().splitlines()[-1]}


def controls(setup: Setup, pins: dict) -> dict:
    failure_slot = setup.slots[manifest.FAILURE_EVENT]
    timer_pin = pins["failing_controls"]["timer-in-ns"]

    def one_step_late(report, first):
        counts = report["channels"][workload.STATE_CHANNEL]
        require(first["kind"] == "missing-actual" and first["observation_ns"] == 0
                and counts["missing_actual"] == len(setup.slots),
                f"input-one-step-late failed for another reason: {first}")

    def timer_in_ns(report, first):
        require(first["kind"] == "value" and first["field"] == timer_pin["field"]
                and first["observation_ns"] == setup.slots[timer_pin["index"]]
                and first["expected"] == int(timer_pin["expected"])
                and first["actual"] == int(timer_pin["actual"]),
                f"timer-in-ns diverged elsewhere than #178 found: {first}")

    def wrong_param(report, first):
        # The observation exists and names its event; a state value differs.
        require(first["kind"] == "value" and first["field"] != "event_ns",
                f"wrong-param failed for another reason: {first}")

    return {
        "input-one-step-late": comparison_control(
            setup, "input-one-step-late", one_step_late),
        "timer-in-ns": comparison_control(setup, "timer-in-ns", timer_in_ns),
        "wrong-param": comparison_control(setup, "wrong-param", wrong_param),
        "crash": run_failure_control(
            setup, "crash", ("'libsafety' exited unexpectedly",)),
        "hang": run_failure_control(
            setup, "hang", ("timeout", f"virtual time {failure_slot} ns"),
            HANG_TIMEOUT_MS),
    }


def resources(result: dict, frames: dict) -> dict:
    wall = min(result["wall_s"])
    return {
        "note": "observational, from one runner; not an acceptance criterion "
                "and not a representative vECU measurement for #125",
        "run_wall_s": result["wall_s"],
        "events_per_s": frames["can_events"] / wall,
        "frames_per_s": frames["received_frames"] / wall,
        "virtual_to_wall": (frames["last_log_mono_ns"]
                            - frames["first_log_mono_ns"]) / 1e9 / wall,
        "nominal_peak_child_rss_kib": result["peak_rss_kib"],
    }


def main(bundle: str, prepared: str, workspace: str) -> None:
    bundle, prepared, workspace = Path(bundle), Path(prepared), Path(workspace)
    inputs, runs, evidence = (workspace / name
                              for name in ("inputs", "runs", "evidence"))
    for directory in (inputs, runs, evidence):
        directory.mkdir(parents=True, exist_ok=True)
    library = bundle / LIBRARY
    pinned = pinned_inputs(bundle, prepared)
    runtime = runtime_identity(library)
    receipts, slots = prepare_inputs(bundle, prepared, inputs, evidence, pinned)
    last_event_ns = (pinned["frames"]["last_log_mono_ns"]
                     - pinned["frames"]["first_log_mono_ns"])
    setup = Setup(library=library, segment=segment(pinned, last_event_ns),
                  window=inputs / "window.mcap",
                  reference=inputs / "reference.mcap",
                  contract=inputs / "contract.json", slots=slots,
                  runs=runs, evidence=evidence)
    result = nominal(setup)
    report = {
        "claim": "public-artifact acceptance of one shared library against "
                 "its independent reference; receive side only; not "
                 "production-vehicle validation",
        "identities": {
            "library": {"path": str(LIBRARY), "sha256": sha256(library)},
            "sources": pinned["sources"],
            "bundle": {"bundle_json_sha256": pinned["bundle_json_sha256"],
                       "tools": pinned["bundle_tools"]},
            "calibration": pinned["pins"]["contract"],
            "source_recording": {"path": str(RECORDING),
                                 "sha256": sha256(bundle / RECORDING)},
            "reference": {"path": str(REFERENCE),
                          "sha256": sha256(bundle / REFERENCE)},
            "export": pinned["frames"],
            "export_tool_image": pinned["tool_image"],
            "packet_layout": pinned["packet_layout"],
            "conversion": receipts,
            "runtime": runtime,
        },
        "run": {
            "step_period_ns": workload.STEP_PERIOD_NS,
            "latency_ns": {workload.FRAME_CHANNEL: 0, workload.STATE_CHANNEL: 0},
            "route_capacity": {workload.FRAME_CHANNEL: setup.segment.largest_burst},
            "duration_ns": workload.duration_ns(last_event_ns),
            "initial_state": "set_safety_hooks(mode, param), then "
                             "set_alternative_experience; nothing seeded",
            "timer": "((first_log_mono_ns + event_ns) // 1000) % 0xFFFFFFFF",
            "warm_up": "window warm-up empty; safety_tick only more than 1 s "
                       "from the first and the last event, compared",
        },
        "nominal": {k: v for k, v in result.items()
                    if k not in ("wall_s", "peak_rss_kib")},
        "contract_sha256": sha256(setup.contract),
        "controls": controls(setup, pinned["pins"]),
        "resources": resources(result, pinned["frames"]),
    }
    write_json(evidence / "report.json", report)
    print(json.dumps({"nominal": report["nominal"]["recording_sha256"],
                      "observations": result["observations_checked"],
                      "controls": {k: v.get("first_divergence") or v.get("diagnostic")
                                   for k, v in report["controls"].items()}},
                     indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
