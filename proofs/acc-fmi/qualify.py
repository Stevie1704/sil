"""Executable interoperability gate; checks remain active under python -O."""
import importlib.metadata
import json
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import fmpy
from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records
from cases import ABS_TOL, REL_TOL, CASES, INPUTS, OUTPUTS, STEPS, STEP_NS
from expected import check, expected

from proof_support import (PARTICIPANT_TIMEOUT_MS, compare_files, file_sha256,
                           require, run_logged, write_json)

HERE = Path(__file__).resolve().parent


def case_manifest(name, case):
    model = case.model
    manifest = Manifest(duration_ns=STEP_NS * STEPS)
    manifest.add_schemas({direction: {"fields": [{"name": n, "type": "f64"} for n in names]}
                         for direction, names in [("Input", INPUTS[model]), ("Output", OUTPUTS[model])]})
    for i in range(len(case.instances)):
        manifest.add_channel(f"input{i}", schema="Input", latency_ns=0)
        manifest.add_channel(f"output{i}", schema="Output")
    manifest.add_process("stimulus", command=["python3", str(HERE / "stimulus.py"), name],
                         step_period_ns=STEP_NS,
                         publishes=[f"input{i}" for i in range(len(case.instances))])
    for i, instance in enumerate(case.instances):
        starts = [part for n, v in zip(INPUTS[model], instance.start)
                  for part in ("--start", f"{n}={v}")]
        binds = [
            part
            for channel, fields in ((f"input{i}", INPUTS[model]),
                                    (f"output{i}", OUTPUTS[model]))
            for field in fields
            for part in ("--bind", f"{channel}:{field}={field}")
        ]
        manifest.add_process(f"fmu{i}", command=["python3", "-m", "sil.fmi",
                             f"/fmus/{model}.fmu", *binds, *starts], step_period_ns=STEP_NS,
                             publishes=[f"output{i}"], priority=1,
                             subscribes=[SubscriberRoute(f"input{i}", capacity=2)])
    return manifest


def inspect_archives(out):
    summary = {}
    # The FMI 3.0 schema is the complete schema tree shipped by pinned FMPy.
    schema = Path(fmpy.__file__).parent / "schema/fmi3"
    write_json(out / "schema-identity.json", {str(p.relative_to(schema)): file_sha256(p)
               for p in sorted(schema.rglob("*.xsd"))})
    for model in INPUTS:
        archive = Path(f"/fmus/{model}.fmu")
        fmpy.read_model_description(archive, validate=True)
        with zipfile.ZipFile(archive) as opened, tempfile.TemporaryDirectory() as temporary:
            opened.extractall(temporary)
            root = ET.fromstring(opened.read("modelDescription.xml"))
            binary = Path(temporary) / f"binaries/x86_64-linux/{model}.so"
            libraries = subprocess.check_output(["ldd", binary], text=True)
            require("not found" not in libraries, libraries)
            (out / f"{model}.ldd.txt").write_text(libraries)
            symbols = subprocess.check_output(["nm", "-D", "--defined-only", binary], text=True)
            names = sorted(line.split()[-1] for line in symbols.splitlines()
                           if line.split()[-1].startswith("fmi3"))
            for required in ("InstantiateCoSimulation", "EnterInitializationMode",
                             "ExitInitializationMode", "SetFloat64", "GetFloat64",
                             "DoStep", "Terminate", "FreeInstance"):
                require(f"fmi3{required}" in names, f"{model}: missing fmi3{required}")
            summary[model] = {"archive_sha256": file_sha256(archive),
                              "schema_valid": True,
                              "capabilities": root.find("CoSimulation").attrib,
                              "exported_symbols": names}
        shutil.copy(archive, out)
        shutil.copy(Path(f"/fmus/{model}.identity.json"), out)
    write_json(out / "archives.json", summary)


def capture_environment(out):
    require(platform.system() == "Linux" and platform.machine() == "x86_64",
            "This proof requires Linux x86-64")
    require(PARTICIPANT_TIMEOUT_MS > 0, "Participant timeout must be positive")
    libpython = Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY")
    write_json(out / "environment.json", {
        "machine": [platform.system(), platform.machine(), *platform.libc_ver()],
        "python": sys.version, "packages": {d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions()},
        "source_revision": Path("/src/source-revision.txt").read_text().strip(),
        "runner_sha256": file_sha256(Path("/build/sil-run")),
        "libpython": {"path": str(libpython), "sha256": file_sha256(libpython)},
        "tolerances": {"absolute": ABS_TOL, "relative": REL_TOL},
        "participant_timeout_ms": PARTICIPANT_TIMEOUT_MS,
    })


def verify_rebuild(out):
    results = {}
    with tempfile.TemporaryDirectory() as temporary:
        run_logged([sys.executable, HERE / "build.py", temporary], out / "rebuild.log")
        for model in INPUTS:
            hashes = compare_files(Path(f"/fmus/{model}.fmu"), Path(temporary) / f"{model}.fmu")
            results[model] = {"build_sha256": hashes, "byte_identical": True}
    write_json(out / "rebuild.json", results)


def verify_null_resource_rejection(out):
    rejected = subprocess.run(
        [sys.executable, str(HERE / "initialization.py"), "controller",
         str(out / "null-resource-initialization.json")],
        capture_output=True, text=True, timeout=30,
    )
    (out / "null-resource-path.log").write_text(rejected.stdout + rejected.stderr)
    require(rejected.returncode == 1, "null resource path did not fail with exit 1")
    require("fmi3InstantiateCoSimulation returned no instance" in rejected.stderr,
            "null resource path failed for a different reason")
    # The successful-lifecycle output is deliberately absent on this failure.
    write_json(out / "null-resource-initialization.json", {
        "phase": "instantiate_rejected", "exit_code": rejected.returncode,
        "diagnostic": "fmi3InstantiateCoSimulation returned no instance",
    })


def check_recording(name, case, recording):
    trace, seen = [], set()
    outputs = {f"output{i}": i for i in range(len(case.instances))}
    for topic, t, payload in read_records(recording):
        if topic not in outputs:
            continue
        i = outputs[topic]
        step = t // STEP_NS
        require(t == step * STEP_NS and 0 <= step < STEPS, f"invalid Slot: {topic}@{t}")
        require((i, step) not in seen, f"duplicate output: {topic}@{t}")
        seen.add((i, step))
        inputs = case.instances[i].inputs_at(step)
        time = (t + STEP_NS) / 1e9
        values = list(struct.unpack("<" + "d" * len(OUTPUTS[case.model]), payload))
        check(values, expected(case.model, inputs, time), f"SiL {name}/{i}@{t}")
        trace.append(dict(instance=i, slot_ns=t, communication_time=time,
                          inputs=inputs, values=values))
    require(seen == {(i, step) for i in range(len(case.instances)) for step in range(STEPS)},
            f"{name}: missing output samples")
    return trace


def qualify_case(name, case, out):
    run_logged([sys.executable, HERE / "independent.py", name, out / f"{name}.fmpy.json"],
               out / f"{name}.fmpy.log")
    run_logged([sys.executable, HERE / "initialization.py", name,
                out / f"{name}.sil-lifecycle.json", "--resources"],
               out / f"{name}.sil-lifecycle.log")
    path = out / f"{name}.json"
    manifest_hash = case_manifest(name, case).write(path).hash
    recordings = [out / f"{name}-{repeat}.mcap" for repeat in (1, 2)]
    for recording in recordings:
        run_logged(["/build/sil-run", path, "-o", recording,
                    "--participant-timeout-ms", str(PARTICIPANT_TIMEOUT_MS)],
                   recording.with_suffix(".log"))
    hashes = compare_files(*recordings)
    trace = check_recording(name, case, recordings[0])
    write_json(out / f"{name}.sil.json", trace)
    return {"manifest_sha256": manifest_hash,
            "recordings": [{"file": path.name, "sha256": sha, "exit_code": 0}
                           for path, sha in zip(recordings, hashes)],
            "byte_deterministic": True, "checked_outputs": len(trace)}


def run(out):
    out.mkdir(parents=True, exist_ok=True)
    capture_environment(out)
    verify_rebuild(out)
    inspect_archives(out)
    run_logged([sys.executable, "-m", "pytest", HERE / "test_expected.py",
                HERE / "test_build.py", HERE / "test_gate.py", "-q"],
               out / "gate-tests.log")
    verify_null_resource_rejection(out)
    results = {name: qualify_case(name, case, out) for name, case in CASES.items()}
    write_json(out / "results.json", results)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
