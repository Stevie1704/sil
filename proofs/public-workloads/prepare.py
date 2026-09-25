"""Qualify the public workloads and write their offline acceptance bundle.

Runs inside the pinned tool image with no network: the sources were fetched
and verified while the image was built. Usage: prepare.py <work-directory>.
The bundle holds only artifacts a later acceptance Run consumes, each with a
digest in `bundle.json`; the evidence holds the audits and results.
Every check raises, so a failed qualification cannot write a passing report.
"""
import hashlib
import importlib.metadata
import json
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

import can_recording
import fmu_workloads
import openacc
import reference_fmus
from libsafety_workload import first_divergence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCES = Path("/sources")
FMUS = Path("/fmus")
OPENDBC = SOURCES / "opendbc"

# Fixed before execution. The recording must carry exactly this contract.
CAN_CONTRACT = {"platform": "TOYOTA_RAV4_TSS2", "mode": 2, "mode_name": "toyota",
                "param": 73, "alternative_experience": 0}
WINDOW = openacc.Window(start_s=300.0, end_s=350.0, lead=1, ego=2)
# The plant's authored initial state; the measured lead acceleration starts here.
PLANT_LEAD_SPEED_MPS = 25.0
MANEUVER_TOL_MPS = 1e-9
KPI = {"minimum_gap_m": 5.0, "acceleration_min_mps2": -3.0, "acceleration_max_mps2": 1.5}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout


def tool_identity():
    return {
        "machine": platform.machine(),
        "libc": " ".join(platform.libc_ver()),
        "python": platform.python_version(),
        "compiler": command("cc", "--version").splitlines()[0],
        "source_revision": (ROOT / "source-revision.txt").read_text().strip(),
        "packages": {name: importlib.metadata.version(name)
                     for name in ("fmpy", "pycapnp", "zstandard", "cffi", "numpy", "pythonfmu3")},
    }


# Shared library ---------------------------------------------------------------

def build_libsafety(bundle):
    """Two builds by the upstream build function; the first one is pinned."""
    sys.path.insert(0, str(OPENDBC))
    from opendbc.safety.tests.libsafety import libsafety_py
    first, second = (libsafety_py._build_libsafety(release=True) for _ in range(2))
    target = bundle / "libsafety" / "libsafety.so"
    target.parent.mkdir(parents=True)
    shutil.copy(first, target)
    harness = OPENDBC / "opendbc/safety/tests/libsafety"
    exported = command("nm", "-D", "--defined-only", target).split("\n")
    return target, {
        "library_sha256": sha256(target),
        "rebuild_identical": sha256(first) == sha256(second),
        "build_function": "opendbc.safety.tests.libsafety.libsafety_py._build_libsafety(release=True)",
        "harness_sha256": {name: sha256(harness / name) for name in ("libsafety_py.py", "safety.c")},
        "runtime_libraries": command("ldd", target).split("\n"),
        "exported_functions": sorted(line.split()[-1] for line in exported if " T " in line),
    }


def run_variant(library, variant, work):
    output = work / f"libsafety-{variant}.json"
    started = time.perf_counter()
    subprocess.run([sys.executable, HERE / "libsafety_workload.py", library, OPENDBC,
                    SOURCES / "can_segment", variant, output], check=True)
    elapsed = time.perf_counter() - started
    return json.loads(output.read_text()), elapsed


UPSTREAM = """
import sys
sys.path.insert(0, sys.argv[2])
from opendbc.safety.tests.libsafety import libsafety_py
libsafety_py.load(sys.argv[1])
from opendbc.car.logreader import LogReader
from opendbc.safety.tests.safety_replay.replay_drive import replay_drive
print("passed:", replay_drive(list(LogReader(sys.argv[3])), *map(int, sys.argv[4:])))
"""


def upstream_replay(library, contract):
    """The upstream replay driver, run as published, over the pinned segment."""
    out = subprocess.run([sys.executable, "-c", UPSTREAM, library, OPENDBC, SOURCES / "can_segment",
                          str(contract["mode"]), str(contract["param"]),
                          str(contract["alternative_experience"])],
                         check=True, capture_output=True, text=True).stdout

    def field(label):
        found = re.search(rf"^{label}: (.*)$", out, re.MULTILINE)
        require(found is not None, f"upstream replay printed no {label!r}:\n{out}")
        return found.group(1)
    return {"received": int(field("total rx msgs")), "invalid": int(field("invalid rx msgs")),
            "tick_invalid": field("safety tick rx invalid") == "True",
            "transmitted": int(field("total openpilot msgs")), "passed": field("passed") == "True"}


def panda_agreement(trace):
    """Recorded in-vehicle controls-allowed against the replayed state."""
    states = can_recording.panda_states(SOURCES / "can_segment")
    agree = index = 0
    for t, allowed, _ in states:
        while index + 1 < len(trace) and trace[index + 1]["t_ns"] <= t:
            index += 1
        agree += trace[index]["controls_allowed"] == allowed
    require(states, "the segment has no recorded panda state")
    return {"samples": len(states), "agreeing": agree,
            "modes": sorted({mode for _, _, mode in states})}


def shared_library(bundle, evidence, work):
    library, build = build_libsafety(bundle)
    inspection = can_recording.inspect(SOURCES / "can_segment")
    require(inspection["transmit_events"] == 0 and set(inspection["event_kinds"]) ==
            {"can", "carParams", "pandaStates"}, "segment carries an undecoded event kind")
    first, first_s = run_variant(library, "nominal", work)
    second, _ = run_variant(library, "nominal", work)
    require(first["contract"] == CAN_CONTRACT, f"recorded contract {first['contract']}")
    trace = first["trace"]
    require(first_divergence(trace, second["trace"]) is None, "two standalone replays differ")
    require(first["config_valid"] and not first["rejected"], "nominal replay rejected frames")
    upstream = upstream_replay(library, CAN_CONTRACT)
    received = sum(row["accepted"] + row["rejected"] for row in trace)
    require(upstream["passed"] and upstream["received"] == received and upstream["invalid"] == 0
            and not upstream["tick_invalid"], f"upstream replay disagrees: {upstream}")
    controls = panda_agreement(trace)
    require(controls["agreeing"] == controls["samples"], f"recorded panda state: {controls}")
    require(first["same_path_dlopen_shares_state"], "same-path loads no longer share state")
    variants = {}
    # Each control must fail for its own reason: a timing fault rejects no
    # frame and loses validity; the corrupted checksum rejects exactly 0x260.
    reasons = {"timer-in-ns": {}, "corrupt-0x260": {"0:0x260"}}
    for variant, rejected in reasons.items():
        result, _ = run_variant(library, variant, work)
        divergence = first_divergence(trace, result["trace"])
        require(divergence is not None and not result["config_valid"]
                and set(result["rejected"]) == set(rejected), f"{variant} was not detected: "
                f"divergence {divergence}, rejected {result['rejected']}")
        variants[variant] = {"first_divergence": divergence, "config_valid": False,
                             "rejected": result["rejected"]}
    reference = bundle / "references" / "libsafety-states.json"
    write_json(reference, {"contract": CAN_CONTRACT, "trace": trace})
    recording = bundle / "recordings" / "rlog.zst"
    recording.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SOURCES / "can_segment", recording)
    write_json(evidence / "can-segment.json", inspection)
    return inspection, {
        "build": build,
        "contract": CAN_CONTRACT,
        "observations": len(trace),
        "received_frames": received,
        "final_observation_ns": trace[-1]["t_ns"],
        "trace_sha256": sha256(reference),
        "standalone_repeat_identical": True,
        "upstream_replay": upstream,
        "recorded_panda_controls_allowed": controls,
        "same_path_dlopen_shares_state": first["same_path_dlopen_shares_state"],
        "failing_variants": variants,
        "observed": {"nominal_replay_wall_s": round(first_s, 3),
                     "received_frames_per_wall_s": round(received / first_s)},
    }


# FMUs -----------------------------------------------------------------------

def recorded_window(bundle, evidence):
    recording = openacc.parse((SOURCES / "openacc_recording").read_text())
    write_json(evidence / "openacc-recording.json", openacc.inspect(recording))
    rows = openacc.window_rows(recording, WINDOW)
    inputs = openacc.controller_inputs(recording, WINDOW)
    accelerations = openacc.lead_acceleration(recording, WINDOW)
    directory = bundle / "recordings"
    directory.mkdir(parents=True, exist_ok=True)
    columns = ("Time", "Speed1", "Speed2", "IVS1")
    lines = [",".join((*columns, "t_ns", *fmu_workloads.CONTROLLER_INPUTS, "lead_accel_mps2"))]
    for row, sample, accel in zip(rows, inputs, [*accelerations, ""]):
        lines.append(",".join(map(repr, [row[c] for c in columns])) + "," + ",".join(
            [str(sample["t_ns"]), *(repr(sample[n]) for n in fmu_workloads.CONTROLLER_INPUTS),
             repr(accel) if accel != "" else ""]))
    (directory / "openacc-window.csv").write_text("\n".join(lines) + "\n")
    shutil.copy(SOURCES / "openacc_copyright", directory / "openacc-copyright.txt")
    shutil.copy(SOURCES / "openacc_notes", directory / "openacc-notes.txt")
    (directory / "openacc-ATTRIBUTION.txt").write_text(
        "Source: JRC OpenACC, AstaZero campaign, ASta_040719_platoon7.csv\n"
        "https://data.jrc.ec.europa.eu/dataset/9702c950-c80f-4d2f-982f-44d06ea0009f\n"
        "(c) European Union, 1995-2026. Licensed under CC BY 4.0.\n"
        f"Changes: rows {WINDOW.start_s} s to {WINDOW.end_s} s inclusive; columns Time, Speed1,\n"
        "Speed2, IVS1 kept unchanged; added t_ns, gap_m, relative_speed_mps,\n"
        "ego_speed_mps and lead_accel_mps2 derived as documented in openacc.py.\n")
    return rows, inputs, accelerations


def control_law():
    """The repository's own ACC law, the source the controller FMU exports."""
    sys.path.insert(0, str(ROOT / "python" / "src"))
    from sil.examples.acc.dynamics import command_for
    return command_for


def single_fmu(bundle, inputs):
    archive = FMUS / "AccController.fmu"
    started = time.perf_counter()
    trace = fmu_workloads.single(archive, inputs)
    elapsed = time.perf_counter() - started
    command_for = control_law()
    law = [{"t_ns": row["t_ns"],
            "accel_mps2": command_for(**{n: sample[n] for n in fmu_workloads.CONTROLLER_INPUTS})}
           for row, sample in zip(trace, inputs)]
    tol = (fmu_workloads.ABS_TOL, fmu_workloads.REL_TOL)
    require(fmu_workloads.numeric_divergence(law, trace, *tol) is None, "FMU departs from its law")
    require(trace == fmu_workloads.single(archive, inputs), "two FMPy executions differ")
    shifted = fmu_workloads.single(archive, fmu_workloads.shifted(inputs, 1))
    divergence = fmu_workloads.numeric_divergence(trace, shifted, *tol)
    require(divergence is not None, "a one-period input shift was not detected")
    reference = bundle / "references" / "controller-open-loop.json"
    write_json(reference, {"window": WINDOW.__dict__, "trace": trace})
    return {"observations": len(trace), "first_observation_ns": trace[0]["t_ns"],
            "final_observation_ns": trace[-1]["t_ns"], "trace_sha256": sha256(reference),
            "matches_control_law": True, "repeat_identical": True,
            "command_range_mps2": [min(r["accel_mps2"] for r in trace),
                                   max(r["accel_mps2"] for r in trace)],
            "failing_variant": {"one-period-input-shift": divergence},
            "observed": {"steps_per_wall_s": round(len(trace) / elapsed)}}


def maneuver_divergence(trace, rows):
    """Plant lead speed at every recorded sample against the recorded change."""
    per_sample = openacc.PERIOD_MS * 1_000_000 // fmu_workloads.LOOP_PERIOD_NS
    initial = rows[0]["Speed1"]
    reference, actual = [], []
    for k in range(1, len(rows)):
        # Slot k*10-1 publishes the state at the end of its step: sample k.
        slot = trace[k * per_sample - 1]
        reference.append({"t_ns": slot["t_ns"],
                          "lead_speed_mps": PLANT_LEAD_SPEED_MPS + rows[k]["Speed1"] - initial})
        actual.append({"t_ns": slot["t_ns"], "lead_speed_mps": slot["lead_speed_mps"]})
    return fmu_workloads.numeric_divergence(reference, actual, MANEUVER_TOL_MPS, 0.0)


def coupled_fmus(bundle, rows, accelerations):
    controller, plant = FMUS / "AccController.fmu", FMUS / "AccPlant.fmu"
    started = time.perf_counter()
    trace = fmu_workloads.coupled(controller, plant, accelerations)
    elapsed = time.perf_counter() - started
    require(maneuver_divergence(trace, rows) is None, "measured lead maneuver not reproduced")
    minimum_gap = min(r["gap_m"] for r in trace)
    commands = [r["accel_mps2"] for r in trace]
    require(minimum_gap >= KPI["minimum_gap_m"], f"minimum gap {minimum_gap}")
    require(KPI["acceleration_min_mps2"] <= min(commands) and
            max(commands) <= KPI["acceleration_max_mps2"], "command outside its clamp")
    require(min(min(r["ego_speed_mps"], r["lead_speed_mps"]) for r in trace) >= 0, "negative speed")
    require(trace == fmu_workloads.coupled(controller, plant, accelerations), "loop not repeatable")
    shifted = fmu_workloads.coupled(controller, plant, fmu_workloads.shifted(accelerations, 1))
    divergence = maneuver_divergence(shifted, rows)
    require(divergence is not None, "a one-sample maneuver shift was not detected")
    reference = bundle / "references" / "coupled-measured-lead.json"
    write_json(reference, {"window": WINDOW.__dict__, "trace": trace})
    return {"slots": len(trace), "final_slot_ns": trace[-1]["t_ns"],
            "trace_sha256": sha256(reference), "repeat_identical": True,
            "maneuver_reproduced": True, "minimum_gap_m": minimum_gap,
            "command_range_mps2": [min(commands), max(commands)],
            "lead_accel_range_mps2": [min(accelerations), max(accelerations)],
            "failing_variant": {"one-sample-maneuver-shift": divergence},
            "observed": {"loop_steps_per_wall_s": round(len(trace) / elapsed)}}


def fmus(bundle):
    audits = {}
    for model in ("AccController", "AccPlant"):
        audits[model] = fmu_workloads.audit(FMUS / f"{model}.fmu")
        require(not audits[model]["unresolved_libraries"], f"{model}: unresolved library")
        (bundle / "fmus").mkdir(parents=True, exist_ok=True)
        shutil.copy(FMUS / f"{model}.fmu", bundle / "fmus")
        shutil.copy(FMUS / f"{model}.identity.json", bundle / "fmus")
    return audits


# Bundle ---------------------------------------------------------------------

def handoff(digest, inspection, library, single, coupled):
    """The identities and contracts #193 and #194 execute against."""
    from libsafety_workload import STATE, VARIANTS
    return {
        "claim": "public-artifact adoption acceptance; not production-vehicle validation",
        "shared_library_193": {
            "library": {"path": "libsafety/libsafety.so", "sha256": digest["libsafety/libsafety.so"],
                        "runtime_libraries": library["build"]["runtime_libraries"]},
            "api": {"init": ["set_safety_hooks(mode, param) == 0",
                             "set_alternative_experience(alternative_experience)"],
                    "per_event": ["set_timer(timer_us(log_mono_ns))",
                                  "safety_tick() and safety_config_valid() when warm",
                                  "per received frame: safety_fwd_hook(bus, address), "
                                  "safety_rx_hook(CANPacket_t) -> accepted"],
                    "state": list(STATE), "shutdown": "none; unload with the process"},
            "contract": CAN_CONTRACT,
            "instance_isolation": "one library instance per Process participant: C globals, "
                                  "and a second dlopen of the same file shares them",
            "recording": {"path": "recordings/rlog.zst", "sha256": digest["recordings/rlog.zst"],
                          "first_log_mono_ns": inspection["first_log_mono_ns"],
                          "can_events": inspection["can_events"],
                          "event_interval_ns": inspection["event_interval_ns"],
                          "received_frames_per_event": inspection["received_frames_per_event"],
                          "max_payload_bytes": max(map(int, inspection["payload_bytes"])),
                          "transmit_coverage": "none: the segment has no sendcan events"},
            "channel_conversion": {
                "virtual_time_ns": "logMonoTime - first_log_mono_ns, not requantized",
                "burst": "one can event = one Burst of its received frames, recorded order",
                "frame": "address u32, bus u8 (src % 4, src < 128), length u8, data 8 bytes",
                "timer": "set_timer(timer_us(first_log_mono_ns + virtual_time_ns))"},
            "latency": "a Burst is processed at the instant it names (Latency 0 or equivalent); "
                       "a one-period delay is a timing variant, not the reference",
            "observation": "after each event's frames: accepted, rejected and every state field",
            "comparison": "exact equality per field and event; all events including the final one",
            "warm_up": "safety_tick only when more than 1 s from both the first and the "
                       "last can event of the segment",
            "reference": {"path": "references/libsafety-states.json",
                          "sha256": digest["references/libsafety-states.json"],
                          "time": "t_ns is the recorded logMonoTime; subtract "
                                  "first_log_mono_ns for virtual time",
                          "observations": library["observations"]},
            "failing_controls": {name: library["failing_variants"][name]["first_divergence"]
                                 for name in VARIANTS if name != "nominal"},
        },
        "single_fmu_194": {
            "fmu": {"path": "fmus/AccController.fmu", "sha256": digest["fmus/AccController.fmu"],
                    "origin": "repository-authored, PythonFMU3 0.3.4 export of proofs/acc-fmi"},
            "recording": {"path": "recordings/openacc-window.csv",
                          "sha256": digest["recordings/openacc-window.csv"],
                          "window": WINDOW.__dict__, "samples": single["observations"]},
            "period_ns": fmu_workloads.RECORDED_PERIOD_NS,
            "duration_ns": single["final_observation_ns"],
            "start_values": "sample 0, set before initialization",
            "input_policy": "sample k is set at t_k (input Latency 0) and held for [t_k, t_k+1)",
            "observation": "the output published in Slot t_k describes t_k + period; compare it "
                           "with the reference row t_ns = t_k + period. The final sample is held "
                           "one period past the recording's end, so the last row is at 50.1 s",
            "comparison": {"absolute": fmu_workloads.ABS_TOL, "relative": fmu_workloads.REL_TOL,
                           "coverage": "every sample including the final one"},
            "parameters": "none: a wrong-parameter control needs a start-value or binding error",
            "reference": {"path": "references/controller-open-loop.json",
                          "sha256": digest["references/controller-open-loop.json"]},
            "failing_controls": single["failing_variant"],
        },
        "coupled_fmus": {
            "fmus": {m: digest[f"fmus/{m}.fmu"] for m in ("AccController", "AccPlant")},
            "period_ns": fmu_workloads.LOOP_PERIOD_NS,
            "latency_ns": {"sensing": fmu_workloads.LOOP_PERIOD_NS,
                           "command": fmu_workloads.LOOP_PERIOD_NS, "maneuver": 0},
            "initial": {"ego_position_m": 0.0, "lead_position_m": 60.0, "ego_speed_mps": 25.0,
                        "lead_speed_mps": PLANT_LEAD_SPEED_MPS, "accel_mps2": 0.0},
            "maneuver": "recorded lead acceleration, forward difference, held per 100 ms sample",
            "maneuver_qualification_mps": MANEUVER_TOL_MPS,
            "ego": "closed loop in the plant; the recorded ego is never replayed",
            "observation": "each reference row t_ns is its publication Slot and describes "
                           "Slot + period; recorded sample k is compared at Slot k * 100 ms - 10 ms",
            "kpi": KPI,
            "reference": {"path": "references/coupled-measured-lead.json",
                          "sha256": digest["references/coupled-measured-lead.json"]},
            "failing_controls": coupled["failing_variant"],
        },
    }


def digests(bundle):
    return {str(p.relative_to(bundle)): sha256(p)
            for p in sorted(bundle.rglob("*")) if p.is_file() and p.name != "bundle.json"}


def main(work):
    require(platform.machine() == "x86_64", "the supported acceptance platform is Linux x86-64")
    bundle, evidence = work / "bundle", work / "evidence"
    for directory in (bundle, evidence):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
    gate = subprocess.run([sys.executable, "-m", "pytest", "-q", HERE], capture_output=True,
                          text=True, cwd=HERE)
    (evidence / "gate-tests.log").write_text(gate.stdout + gate.stderr)
    require(gate.returncode == 0, "gate tests failed")
    inspection, library = shared_library(bundle, evidence, work)
    rows, inputs, accelerations = recorded_window(bundle, evidence)
    audits = fmus(bundle)
    single = single_fmu(bundle, inputs)
    coupled = coupled_fmus(bundle, rows, accelerations)
    write_json(evidence / "fmu-audit.json", audits)
    references = reference_fmus.audit(SOURCES / "reference_fmus")
    write_json(evidence / "reference-fmus.json", references)
    smoked = [entry["fmpy_smoke"] for entry in references.values()
              if entry["inside_qualified_profile"]]
    require(smoked and all(smoke["finite"] for smoke in smoked),
            "no Reference FMU smoke test ran, or one is not finite")
    sources = json.loads((HERE / "sources.json").read_text())
    digest = digests(bundle)
    write_json(bundle / "handoff.json", handoff(digest, inspection, library, single, coupled))
    digest = digests(bundle)
    write_json(bundle / "bundle.json", {"sources": sources, "tools": tool_identity(),
                                        "digests": digest})
    # FMPy runs in this process, the library replays in children.
    peak_kib = {"fmpy_process": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "library_children": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}
    write_json(evidence / "report.json", {
        "bundle_sha256": sha256(bundle / "bundle.json"),
        "shared_library": library,
        "single_fmu": single,
        "coupled_fmus": coupled,
        "observed_peak_rss_kib": peak_kib,
    })


if __name__ == "__main__":
    main(Path(sys.argv[1]))
