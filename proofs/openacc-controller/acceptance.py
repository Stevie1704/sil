"""Qualify the public ACC controller FMU on recorded OpenACC data (#194).

Runs inside the example image with no network and no source tree: SiL comes
from the installed wheel and runner, the FMU, the recording window and the
FMPy reference from the #178 bundle. Every check raises, so a failed
acceptance cannot write a passing report.

Usage: acceptance.py <bundle> <workspace>

The order is fixed. Pins, interface and lifecycle are checked first. Then the
conversion and every expected control failure are written to
`inputs/expectations.json`, before any Run. Only then do the Runs start.

The workspace receives `inputs/` (converted Recordings, mappings, authoring
documents, contract, expectations), `runs/` (Manifests, Recordings,
provenance, runner output) and `evidence/` (receipts, inspection, lifecycle,
comparison reports and `report.json`).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
import sysconfig
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import controller_replay as replay
from identity import archive_identity, audited_interface, inspected_interface

from sil.examples.acc import dynamics

HERE = Path(__file__).resolve().parent
# The #178 evidence as committed: the pins this acceptance consumes.
HANDOFF = HERE / "handoff.json"
AUDIT = HERE / "fmu-audit.json"
# The model source as committed; the archive must carry these bytes.
MODEL_SOURCE = HERE / "controller.py"
FMU = Path("fmus/AccController.fmu")
RECORDING = Path("recordings/openacc-window.csv")
REFERENCE = Path("references/controller-open-loop.json")
BINARY = "binaries/x86_64-linux/AccController.so"
TIMEOUT_MS = int(os.environ.get("SIL_OPENACC_PARTICIPANT_TIMEOUT_MS", "30000"))
FAILING = ("changed-input", "wrong-binding", "one-period-shift")


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
    require(proc.returncode == 0, f"{' '.join(map(str, arguments))} exited "
            f"{proc.returncode}:\n{proc.stderr}")
    return proc.stdout


# Identities ---------------------------------------------------------------------

def without_fmu_digest(pins: dict) -> dict:
    return {**pins, "fmu": {k: v for k, v in pins["fmu"].items() if k != "sha256"}}


def pinned_inputs(bundle: Path) -> dict:
    """The bundle's FMU, recording and reference, against the pins.

    The recording and the reference have the committed digests. The FMU
    digest depends on the revision it was exported at, so the FMU must be
    the bundle's own, and carry the committed model source and the installed
    control law byte for byte."""
    pins = read_json(HANDOFF)["single_fmu_194"]
    listed = read_json(bundle / "bundle.json")
    for path, pin in ((RECORDING, pins["recording"]["sha256"]),
                      (REFERENCE, pins["reference"]["sha256"])):
        require(sha256(bundle / path) == pin == listed["digests"][str(path)],
                f"{path} is not the pinned artifact {pin}")
    regenerated = read_json(bundle / "handoff.json")["single_fmu_194"]
    require(without_fmu_digest(regenerated) == without_fmu_digest(pins),
            "the bundle's single-FMU contract is not the committed one")
    fmu = sha256(bundle / FMU)
    require(fmu == listed["digests"][str(FMU)] == regenerated["fmu"]["sha256"],
            f"{FMU} is not the archive its bundle lists")
    identity = archive_identity(bundle / FMU)
    embedded = identity["embedded"]
    require(identity["model_sha256"] == sha256(MODEL_SOURCE),
            "the archive does not carry the committed model source")
    require(identity["dynamics_sha256"] == sha256(Path(dynamics.__file__)),
            "the archive's control law is not the installed one")
    require(embedded["exporter"] == read_json(AUDIT)["AccController"]["generation_tool"],
            f"exported by {embedded['exporter']}, not the audited exporter")
    return {"pins": pins, "identity": identity,
            "same_archive_as_handoff": fmu == pins["fmu"]["sha256"],
            "sources": {"openacc_recording": listed["sources"]["openacc_recording"]},
            "bundle_tools": listed["tools"],
            "bundle_json_sha256": sha256(bundle / "bundle.json")}


def inspected(fmu: Path, evidence: Path) -> dict:
    """`sil fmi inspect` finds the interface #178 audited, and usable here."""
    report = json.loads(command("sil", "fmi", "inspect", "--json", str(fmu)))
    write_json(evidence / "inspection.json", report)
    require(report["verdict"] == "compatible",
            f"sil fmi inspect: {report['verdict']}: {report['unusable']}")
    interface = inspected_interface(report)
    require(interface == audited_interface(read_json(AUDIT)["AccController"]),
            "the declared interface is not the one #178 audited")
    return {"verdict": report["verdict"], "interface": interface,
            "instantiation_token": report["facts"]["instantiation_token"],
            "not_verified_statically": report["unverified"]}


def runtime_identity(fmu: Path) -> dict:
    """What the FMU needs from the image: its binary's libraries and the
    Python it embeds (`needsExecutionTool`)."""
    with tempfile.TemporaryDirectory() as directory, zipfile.ZipFile(fmu) as opened:
        binary = Path(opened.extract(BINARY, directory))
        ldd = command("ldd", str(binary))
    require("not found" not in ldd, f"the runtime image cannot load it:\n{ldd}")
    libpython = Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY")
    require(libpython.is_file(), f"the image has no {libpython}")
    return {
        "machine": platform.machine(),
        "libc": " ".join(platform.libc_ver()),
        "python": platform.python_version(),
        "libpython": str(libpython),
        "runner": json.loads(command("sil-run", "--build-info")),
        "binary_ldd": [line.split(" (0x")[0].strip() for line in ldd.splitlines()],
        "example_image_id": os.environ.get("SIL_OPENACC_EXAMPLE_IMAGE_ID"),
    }


def checked_lifecycle(fmu: Path, first: dict, evidence: Path) -> dict:
    """Initialization needs the resources, and a full lifecycle ends cleanly."""
    starts = [f"--start={name}={first[name]!r}" for name in replay.INPUTS]

    def lifecycle(name: str, *flags: str):
        out = evidence / f"lifecycle-{name}.json"
        proc = subprocess.run([sys.executable, str(HERE / "lifecycle.py"), str(fmu),
                               str(out), *flags, *starts],
                              capture_output=True, text=True, timeout=60)
        # A process that fails before its first phase may write nothing.
        return proc, read_json(out) if out.is_file() else []

    proc, phases = lifecycle("resources")
    command_0 = dynamics.command_for(**{name: first[name] for name in replay.INPUTS})
    require(proc.returncode == 0 and phases == [
        {"phase": "described"},
        {"phase": "instantiated"},
        {"phase": "initialized", replay.OUTPUT: command_0},
        {"phase": "stepped", "t_ns": replay.PERIOD_NS, replay.OUTPUT: command_0},
        {"phase": "terminated"}],
        f"the lifecycle did not complete as the law gives:\n{phases}\n{proc.stderr}")
    # The same archive, described the same way: only the resource path differs,
    # so a lifecycle that stops after `described` was refused at instantiation.
    rejected, reached = lifecycle("no-resources", "--no-resources")
    require(rejected.returncode != 0 and reached == [{"phase": "described"}],
            f"without resources the FMU was not rejected at instantiation:\n"
            f"{reached}\n{rejected.stderr}")
    return {"with_resources": [p["phase"] for p in phases],
            "initialized_command_mps2": command_0,
            "without_resources": [p["phase"] for p in reached]}


# Conversion ---------------------------------------------------------------------

def convert(document: dict, source: Path, output: Path, evidence: Path) -> dict:
    """One sil recording csv step, with its mapping and receipt kept."""
    mapping = write_json(output.with_suffix(".mapping.json"), document)
    receipt = evidence / f"{output.stem}.receipt.json"
    command("sil", "recording", "csv", str(mapping), str(source), "-o", str(output),
            "--receipt", str(receipt))
    return read_json(receipt)


def prepare_inputs(bundle: Path, pins: dict, inputs: Path, evidence: Path):
    """The checked samples, the converted Recordings and the contract."""
    samples = replay.recorded_inputs((bundle / RECORDING).read_text(),
                                       pins["recording"]["window"]["start_s"])
    require(len(samples) == pins["recording"]["samples"],
            f"the window holds {len(samples)} samples")
    receipts = {}
    for variant in (replay.NOMINAL, replay.CONTROLS["changed-input"]):
        name = replay.recording_stem(variant)
        receipts[name] = convert(replay.input_mapping(variant), bundle / RECORDING,
                                 inputs / f"{name}.mcap", evidence)
        count = receipts[name]["channels"][replay.SENSING_CHANNEL]["messages"]
        require(count == len(samples), f"sil recording csv converted {count} samples")
    trace = read_json(bundle / REFERENCE)["trace"]
    require([row["t_ns"] for row in trace]
            == [(k + 1) * replay.PERIOD_NS for k in range(len(samples))],
            "the reference is not one command per sample, one period later")
    rows = replay.reference_rows(trace)
    with (inputs / "reference.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    receipts["reference"] = convert(replay.REFERENCE_MAPPING, inputs / "reference.csv",
                                    inputs / "reference.mcap", evidence)
    write_json(inputs / "contract.json", replay.comparison_contract())
    return samples, trace, receipts


def expectations(samples: list[dict], trace: list[dict], pins: dict) -> dict:
    """Every Run's expected verdict, from the law, before any Run."""
    expected = {name: replay.predicted_divergence(variant, samples, trace,
                                                    dynamics.command_for)
                for name, variant in {"nominal": replay.NOMINAL,
                                      **replay.CONTROLS}.items()}
    require(expected["nominal"] is None and expected["declared-starts"] is None,
            f"the law departs from the reference: nominal "
            f"{expected['nominal']}, declared-starts {expected['declared-starts']}")
    require(all(expected[name] is not None for name in FAILING),
            f"a control cannot fail: {expected}")
    shift = pins["failing_controls"]["one-period-input-shift"]
    require(expected["one-period-shift"] == {
        "index": shift["index"], "observation_ns": shift["t_ns"],
        "expected": shift["expected"], "actual": shift["actual"]},
        f"the one-period shift diverges elsewhere than #178 found: "
        f"{expected['one-period-shift']}")
    return expected


# Runs ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Workspace:
    """What every Run and comparison of the acceptance share."""

    fmu: Path
    first: dict
    inputs: Path
    runs: Path
    evidence: Path

    def recording(self, variant: replay.Variant) -> Path:
        return self.inputs / f"{replay.recording_stem(variant)}.mcap"


def authored(setup: Workspace, name: str, variant: replay.Variant) -> dict:
    document = write_json(setup.inputs / f"{name}.authoring.json",
                          replay.authoring_document(variant, setup.first))
    manifest = setup.runs / f"{name}.json"
    receipt = setup.evidence / f"{name}.authoring-receipt.json"
    command("sil", "fmi", "replay", str(document), str(setup.fmu),
            "--recording", str(setup.recording(variant)), "-o", str(manifest),
            "--receipt", str(receipt))
    return {"manifest": manifest, "receipt": read_json(receipt)}


def run(setup: Workspace, name: str, manifest: Path) -> dict:
    recording = setup.runs / f"{name}.mcap"
    start = time.perf_counter()
    proc = subprocess.run(
        ["sil-run", str(manifest), "-o", str(recording),
         "--participant-timeout-ms", str(TIMEOUT_MS)],
        capture_output=True, text=True)
    wall_s = time.perf_counter() - start
    (setup.runs / f"{name}.log").write_text(proc.stdout + proc.stderr)
    require(proc.returncode == 0, f"Run {name} failed:\n{proc.stderr}")
    return {"recording": recording, "sha256": sha256(recording), "wall_s": wall_s}


def compare(setup: Workspace, name: str, recording: Path) -> dict:
    proc = subprocess.run(["sil", "compare", str(setup.inputs / "contract.json"),
                           str(recording), str(setup.inputs / "reference.mcap"),
                           "--json"], capture_output=True, text=True)
    require(proc.returncode in (0, 1), f"sil compare could not judge {name}: "
            f"{proc.stderr}")
    report = json.loads(proc.stdout)
    write_json(setup.evidence / f"compare-{name}.json", report)
    return report


def nominal(setup: Workspace, samples: int) -> dict:
    """One Manifest authored twice and run twice: identical bytes, and in
    agreement with the reference at every command."""
    first = authored(setup, "nominal", replay.NOMINAL)
    again = authored(setup, "nominal-again", replay.NOMINAL)
    require(first["manifest"].read_bytes() == again["manifest"].read_bytes(),
            "two authorings of the nominal document differ")
    runs = [run(setup, f"nominal-{i}", first["manifest"]) for i in (1, 2)]
    require(runs[0]["sha256"] == runs[1]["sha256"],
            "two Runs of the nominal Manifest recorded different bytes")
    report = compare(setup, "nominal", runs[0]["recording"])
    counts = report["channels"][replay.COMMAND_CHANNEL]
    require(report["verdict"] == "pass" and counts["checked"] == samples,
            f"the nominal Run does not match the reference: "
            f"{report['first_divergence'] or report['coverage']}")
    return {"manifest_sha256": first["receipt"]["manifest"]["sha256"],
            "manifests_byte_identical": True,
            "recording_sha256": runs[0]["sha256"], "recordings_byte_identical": True,
            "observations_checked": counts["checked"],
            "first_observation_ns": replay.PERIOD_NS,
            "final_observation_ns": replay.DURATION_NS,
            "shutdown": "sil-run exit 0: the importer exited 0 after "
                        "fmi3Terminate and fmi3FreeInstance",
            "authoring": first["receipt"],
            "provenance": read_json(Path(f"{runs[0]['recording']}.provenance.json")),
            "wall_s": [r["wall_s"] for r in runs]}


def control(setup: Workspace, name: str, expected: dict | None) -> dict:
    """One changed Run: it must fail where the law said, or pass if it
    changes nothing observable."""
    result = run(setup, name, authored(setup, name, replay.CONTROLS[name])["manifest"])
    report = compare(setup, name, result["recording"])
    if expected is None:
        require(report["verdict"] == "pass", f"{name} changed the commands: "
                f"{report['first_divergence']}")
        return {"verdict": "pass", "recording_sha256": result["sha256"]}
    first = report["first_divergence"]
    require(report["verdict"] == "fail", f"{name} passed the comparison")
    require(first["kind"] == "value" and first["field"] == replay.OUTPUT
            and first["observation_ns"] == expected["observation_ns"]
            and first["expected"] == expected["expected"]
            and abs(first["actual"] - expected["actual"])
            <= replay.ABS_TOL + replay.REL_TOL * abs(expected["actual"]),
            f"{name} diverged elsewhere than expected {expected}: {first}")
    return {"verdict": "fail", "divergences": report["divergences"],
            "first_divergence": first}


def main(bundle: str, workspace: str) -> None:
    bundle, workspace = Path(bundle), Path(workspace)
    inputs, runs, evidence = (workspace / name for name in ("inputs", "runs", "evidence"))
    for directory in (inputs, runs, evidence):
        directory.mkdir(parents=True, exist_ok=True)
    fmu = bundle / FMU
    pinned = pinned_inputs(bundle)
    pins = pinned["pins"]
    inspection = inspected(fmu, evidence)
    runtime = runtime_identity(fmu)
    samples, trace, receipts = prepare_inputs(bundle, pins, inputs, evidence)
    lifecycle = checked_lifecycle(fmu, samples[0], evidence)
    expected = expectations(samples, trace, pins)
    write_json(inputs / "expectations.json", expected)

    setup = Workspace(fmu=fmu, first=samples[0], inputs=inputs, runs=runs,
                  evidence=evidence)
    result = nominal(setup, len(samples))
    controls = {name: control(setup, name, expected[name])
                for name in replay.CONTROLS}
    report = {
        "claim": "public-artifact acceptance of one FMU on recorded data against "
                 "an independent importer; the model is repository-authored; "
                 "not production-vehicle validation",
        "identities": {
            "fmu": {"path": str(FMU), **pinned["identity"],
                    "same_archive_as_handoff": pinned["same_archive_as_handoff"],
                    "handoff_archive_sha256": pins["fmu"]["sha256"],
                    "origin": pins["fmu"]["origin"]},
            "inspection": inspection,
            "recording": {"path": str(RECORDING), "sha256": sha256(bundle / RECORDING),
                          "window": pins["recording"]["window"],
                          "source": pinned["sources"]["openacc_recording"]},
            "reference": {"path": str(REFERENCE), "sha256": sha256(bundle / REFERENCE),
                          "importer": "FMPy, in the #178 tool image"},
            "bundle": {"bundle_json_sha256": pinned["bundle_json_sha256"],
                       "tools": pinned["bundle_tools"]},
            "conversion": receipts,
            "runtime": runtime,
        },
        "lifecycle": lifecycle,
        "run": {
            "period_ns": replay.PERIOD_NS,
            "duration_ns": replay.DURATION_NS,
            "start_values": "sample 0 of each input",
            "latency_ns": {replay.SENSING_CHANNEL: 0,
                           replay.COMMAND_CHANNEL: replay.PERIOD_NS},
            "observation": "the command published in Slot t_k is compared with "
                           "the reference row t_k + 100 ms",
            "comparison": {"atol": replay.ABS_TOL, "rtol": replay.REL_TOL,
                           "rule": "abs(actual - reference) <= atol + rtol * "
                                   "abs(reference)"},
        },
        "expectations_sha256": sha256(inputs / "expectations.json"),
        "contract_sha256": sha256(inputs / "contract.json"),
        "nominal": {k: v for k, v in result.items() if k != "wall_s"},
        "controls": controls,
        "resources": {
            "note": "observational, from one runner; not an acceptance criterion "
                    "and not a representative vECU measurement for #125",
            "run_wall_s": result["wall_s"],
            "virtual_to_wall": replay.DURATION_NS / 1e9 / min(result["wall_s"]),
        },
    }
    write_json(evidence / "report.json", report)
    print(json.dumps({"nominal": result["recording_sha256"],
                      "observations": result["observations_checked"],
                      "controls": {k: v.get("first_divergence", v["verdict"])
                                   for k, v in controls.items()}}, indent=2))


if __name__ == "__main__":
    main(*sys.argv[1:])
