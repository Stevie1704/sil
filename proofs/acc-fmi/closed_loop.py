"""Closed-loop evidence gate; all comparisons use communication-point identity."""
import json
import math
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records

HERE = Path(__file__).resolve().parent


def require(condition, diagnostic):
    if not condition:
        raise RuntimeError(diagnostic)

STEP_NS = 10_000_000
STEPS = 500
MIN_GAP_M = 5.0
FIELDS = {
    "sensing": ["gap_m", "relative_speed_mps", "ego_speed_mps"],
    "command": ["accel_mps2"],
    "state": ["ego_position_m", "lead_position_m", "lead_speed_mps"],
}
# Predeclared SI tolerances, individually recorded for every signal.
TOLERANCES = {name: (1e-10, 1e-12) for fields in FIELDS.values() for name in fields}
UNITS = {name: ("m/s2" if name == "accel_mps2" else
                "m/s" if "speed" in name else "m") for name in TOLERANCES}


def validate_archives(directory=Path("/fmus")):
    for model, inputs, outputs, starts in (
        ("AccController", FIELDS["sensing"], FIELDS["command"], [60, 0, 25]),
        ("AccPlant", FIELDS["command"], FIELDS["sensing"] + FIELDS["state"], [0]),
    ):
        with zipfile.ZipFile(directory / f"{model}.fmu") as archive:
            root = ET.fromstring(archive.read("modelDescription.xml"))
        variables = {v.attrib["name"]: v for v in root.find("ModelVariables")}
        for causality, names in (("input", inputs), ("output", outputs)):
            actual = {n for n, v in variables.items() if v.get("causality") == causality}
            require(actual == set(names), f"{model}: invalid {causality} names: {actual}")
            for name in names:
                v = variables[name]
                require(v.tag == "Float64" and v.get("unit") == UNITS[name],
                        f"{model}/{name}: invalid scalar type/unit")
        for name, start in zip(inputs, starts):
            require(float(variables[name].get("start")) == start,
                    f"{model}/{name}: invalid start")


def manifest():
    m = Manifest(duration_ns=STEP_NS * STEPS)
    for channel, fields in FIELDS.items():
        m.add_schemas({channel: {"fields": [{"name": n, "type": "f64"} for n in fields]}})
        m.add_channel(channel, schema=channel, latency_ns=STEP_NS)
    for model, name, incoming, outgoing in (
        ("AccPlant", "plant", ["command"], ["sensing", "state"]),
        ("AccController", "controller", ["sensing"], ["command"]),
    ):
        binds = [part for ch in incoming + outgoing for field in FIELDS[ch]
                 for part in ("--bind", f"{ch}:{field}={field}")]
        m.add_process(name, command=["python3", "-m", "sil.fmi", f"/fmus/{model}.fmu", *binds],
                      step_period_ns=STEP_NS, publishes=outgoing,
                      subscribes=[SubscriberRoute(ch, capacity=2) for ch in incoming])
    return m


def recording_samples(path, original=False):
    rows = {}
    channels = {"acc.Sensing": "sensing", "acc.Command": "command"} if original else {
        n: n for n in FIELDS}
    for channel, t, payload in read_records(path):
        require(channel in channels, f"unknown Channel {channel}")
        name = channels[channel]
        require(t % STEP_NS == 0 and 0 <= t < STEPS * STEP_NS, f"invalid timestamp {channel}@{t}")
        row = rows.setdefault(t, {})
        require(name not in row, f"duplicate {channel}@{t}")
        row[name] = list(struct.unpack("<" + "d" * len(FIELDS[name]), payload))
    return rows


def compare(rows, reference, original=False):
    require(set(rows) == {n * STEP_NS for n in range(STEPS)}, "sample count/timestamps differ")
    require(len(reference) == STEPS, "reference sample count differs")
    for n, expected in enumerate(reference):
        t = n * STEP_NS
        require(expected["slot_ns"] == t and expected["communication_ns"] == t + STEP_NS,
                f"reference timestamp mismatch @{t}")
        names = {"sensing", "command"} if original else set(FIELDS)
        if original and n == 0:
            names.remove("command")
        require(set(rows[t]) == names, f"missing/extra signal @{t}: {set(rows[t])}")
        for channel in sorted(names):
            require(len(rows[t][channel]) == len(expected[channel]) == len(FIELDS[channel]),
                    f"width {channel}@{t}")
            for signal, actual, wanted in zip(FIELDS[channel], rows[t][channel], expected[channel]):
                absolute, relative = TOLERANCES[signal]
                require(math.isfinite(actual) and math.isfinite(wanted) and
                        math.isclose(actual, wanted, abs_tol=absolute, rel_tol=relative),
                        f"first mismatch {signal} publication={t} "
                        f"communication={t if original else t + STEP_NS}: "
                        f"SiL={actual}, reference={wanted}")


def execute(m, name, out):
    from qualify import PARTICIPANT_TIMEOUT_MS, compare_files, run_logged
    path = out / f"{name}.json"
    identity = m.write(path).hash
    recordings = [out / f"{name}-{n}.mcap" for n in (1, 2)]
    for recording in recordings:
        run_logged(["/build/sil-run", path, "-o", recording, "--participant-timeout-ms",
                    str(PARTICIPANT_TIMEOUT_MS)], recording.with_suffix(".log"))
    return recordings[0], dict(manifest_sha256=identity, recording_sha256=compare_files(*recordings))


def reject_invalid_bindings(out):
    from qualify import PARTICIPANT_TIMEOUT_MS, write_json
    diagnostics = {}
    for label, variable, diagnostic in (
        ("unknown", "unknown", "does not declare"),
        ("causality", "accel_mps2", "causality"),
    ):
        path = out / f"invalid-{label}.json"
        manifest().write(path)
        document = json.loads(path.read_text())
        command = document["participants"]["controller"]["command"]
        command[command.index("sensing:gap_m=gap_m")] = f"sensing:gap_m={variable}"
        write_json(path, document)
        result = subprocess.run(["/build/sil-run", str(path), "--participant-timeout-ms",
                                 str(PARTICIPANT_TIMEOUT_MS)], capture_output=True, text=True,
                                timeout=max(120, PARTICIPANT_TIMEOUT_MS / 1000 * 4))
        output = result.stdout + result.stderr
        path.with_suffix(".log").write_text(output)
        require(result.returncode == 2 and diagnostic in output,
                f"invalid {label} binding did not fail clearly with exit 2: {output}")
        diagnostics[label] = dict(exit_code=result.returncode, diagnostic=diagnostic)
    return diagnostics


def run(out):
    from qualify import capture_environment, inspect_archives, run_logged, write_json
    out.mkdir(parents=True, exist_ok=True)
    capture_environment(out)
    inspect_archives(out)
    validate_archives()
    run_logged([sys.executable, "-m", "pytest", HERE / "test_closed_loop.py", "-q"],
               out / "gate-tests.log")
    write_json(out / "configuration.json", dict(step_ns=STEP_NS, steps=STEPS,
               tolerances=TOLERANCES, minimum_gap_m=MIN_GAP_M, route_capacity=2,
               latency_ns=STEP_NS, fields=FIELDS, units=UNITS))
    references = {}
    for mode in ("nominal", "shift-command", "shift-sensing", "initial-command", "original"):
        path = out / f"{mode}.fmpy.json"
        run_logged([sys.executable, HERE / "loop_reference.py", mode, path], path.with_suffix(".log"))
        references[mode] = json.loads(path.read_text())["samples"]
    recording, results = execute(manifest(), "closed-loop", out)
    invalid_bindings = reject_invalid_bindings(out)
    rows = recording_samples(recording)
    compare(rows, references["nominal"])
    minimum = min(row["sensing"][0] for row in rows.values())
    require(minimum >= MIN_GAP_M, f"minimum gap {minimum} < {MIN_GAP_M}")
    # These are newly executed FMU trajectories, not edited copies of the oracle.
    rejected = {}
    for mode in ("shift-command", "shift-sensing", "initial-command"):
        try:
            compare(rows, references[mode])
        except RuntimeError as error:
            rejected[mode] = str(error)
        else:
            raise RuntimeError(f"negative control {mode} was not detected")
    require(any(a["command"] != b["command"] for a, b in zip(
        references["nominal"], references["shift-sensing"])), "sensing did not change commands")
    require(any(a["state"] != b["state"] for a, b in zip(
        references["nominal"], references["shift-command"])), "commands did not change motion")
    from sil.examples.acc.manifest import acc_manifest
    original, original_results = execute(acc_manifest(), "original-python", out)
    compare(recording_samples(original, original=True), references["original"], original=True)
    write_json(out / "results.json", dict(closed_loop=results, original_python=original_results,
               minimum_gap_m=minimum, checked_samples=STEPS, negative_controls=rejected,
               invalid_bindings=invalid_bindings))


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
