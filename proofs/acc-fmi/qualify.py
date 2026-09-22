"""Executable interoperability gate; any missing/invalid evidence fails closed."""
import hashlib
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

from fmpy import read_model_description
from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records
from cases import ABS_TOL, REL_TOL, CASES, INPUTS, OUTPUTS, STEPS, STEP_NS
from expected import check, expected

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def command(args, log):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=120)
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"exit {result.returncode}: {args}; see {log}")


def manifest(case):
    model = case["model"]
    m = Manifest(duration_ns=STEP_NS * STEPS)
    m.add_schemas({direction: {"fields": [{"name": n, "type": "f64"} for n in names]}
                   for direction, names in [("Input", INPUTS[model]), ("Output", OUTPUTS[model])]})
    for i in range(len(case["instances"])):
        m.add_channel(f"input{i}", schema="Input", latency_ns=0)
        m.add_channel(f"output{i}", schema="Output")
    name = next(name for name, spec in CASES.items() if spec is case)
    m.add_process("stimulus", command=["python3", str(HERE / "stimulus.py"), name],
                  step_period_ns=STEP_NS, publishes=["input0", "input1"])
    for i, spec in enumerate(case["instances"]):
        starts = [part for n, v in zip(INPUTS[model], spec["start"])
                  for part in ("--start", f"{n}={v}")]
        m.add_process(f"fmu{i}", command=["python3", "-m", "sil.fmi",
                      f"/fmus/{model}.fmu", *starts], step_period_ns=STEP_NS,
                      publishes=[f"output{i}"], priority=1,
                      subscribes=[SubscriberRoute(f"input{i}", capacity=2)])
    return m


def inspect_archives(out):
    summary = {}
    # The FMI 3.0 schema is the complete schema tree shipped by pinned FMPy.
    import fmpy
    schema = Path(fmpy.__file__).parent / "schema/fmi3"
    write(out / "schema-identity.json", {str(p.relative_to(schema)): digest(p)
          for p in sorted(schema.rglob("*.xsd"))})
    for model in INPUTS:
        archive = Path(f"/fmus/{model}.fmu")
        read_model_description(archive, validate=True)
        with zipfile.ZipFile(archive) as z, tempfile.TemporaryDirectory() as temporary:
            z.extractall(temporary)
            root = ET.fromstring(z.read("modelDescription.xml"))
            binary = Path(temporary) / f"binaries/x86_64-linux/{model}.so"
            libraries = subprocess.check_output(["ldd", binary], text=True)
            if "not found" in libraries:
                raise AssertionError(libraries)
            (out / f"{model}.ldd.txt").write_text(libraries)
            symbols = subprocess.check_output(["nm", "-D", "--defined-only", binary], text=True)
            names = sorted(line.split()[-1] for line in symbols.splitlines()
                           if line.split()[-1].startswith("fmi3"))
            for required in ("InstantiateCoSimulation", "EnterInitializationMode",
                             "ExitInitializationMode", "SetFloat64", "GetFloat64",
                             "DoStep", "Terminate", "FreeInstance"):
                assert f"fmi3{required}" in names, required
            summary[model] = {"archive_sha256": digest(archive),
                              "schema_valid": True,
                              "capabilities": root.find("CoSimulation").attrib,
                              "exported_symbols": names}
        shutil.copy(archive, out)
        shutil.copy(Path(f"/fmus/{model}.identity.json"), out)
    write(out / "archives.json", summary)


def run(out):
    out.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("This proof requires Linux x86-64")
    write(out / "environment.json", {
        "machine": [platform.system(), platform.machine(), *platform.libc_ver()],
        "python": sys.version, "packages": {d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions()},
        "source_revision": Path("/src/source-revision.txt").read_text().strip(),
        "runner_sha256": digest(Path("/build/sil-run")),
        "libpython": {"path": str(Path(sysconfig.get_config_var("LIBDIR")) /
                                   sysconfig.get_config_var("LDLIBRARY")),
                      "sha256": digest(Path(sysconfig.get_config_var("LIBDIR")) /
                                       sysconfig.get_config_var("LDLIBRARY"))},
        "tolerances": {"absolute": ABS_TOL, "relative": REL_TOL},
    })
    # Independent clean exporter invocations must produce the same archive bytes.
    with tempfile.TemporaryDirectory() as temporary:
        command([sys.executable, HERE / "build.py", temporary], out / "rebuild.log")
        for model in INPUTS:
            assert Path(f"/fmus/{model}.fmu").read_bytes() == (Path(temporary) / f"{model}.fmu").read_bytes()
    inspect_archives(out)
    command([sys.executable, "-m", "pytest", HERE / "test_expected.py", "-q"],
            out / "oracle-negative-tests.log")
    results = {}
    for name, case in CASES.items():
        command([sys.executable, HERE / "independent.py", name, out / f"{name}.fmpy.json"],
                out / f"{name}.fmpy.log")
        command([sys.executable, HERE / "initialization.py", name,
                 out / f"{name}.sil-initialization.json", "--resources"],
                out / f"{name}.sil-initialization.log")
        path = out / f"{name}.json"
        manifest_hash = manifest(case).write(path).hash
        recordings = [out / f"{name}-{repeat}.mcap" for repeat in (1, 2)]
        for recording in recordings:
            command(["/build/sil-run", path, "-o", recording,
                     "--participant-timeout-ms", "30000"], recording.with_suffix(".log"))
        assert recordings[0].read_bytes() == recordings[1].read_bytes(), name
        held = [instance["start"] for instance in case["instances"]]
        trace = []
        seen = set()
        for topic, t, payload in read_records(recordings[0]):
            if not topic.startswith("output"):
                continue
            i = int(topic[-1])
            step = t // STEP_NS
            assert t == step * STEP_NS and 0 <= step < STEPS
            assert (i, step) not in seen
            seen.add((i, step))
            held[i] = case["instances"][i]["updates"].get(str(step), held[i])
            time = (t + STEP_NS) / 1e9
            values = list(struct.unpack("<" + "d" * len(OUTPUTS[case["model"]]), payload))
            check(values, expected(case["model"], held[i], time), f"SiL {name}/{i}@{t}")
            trace.append(dict(instance=i, slot_ns=t, communication_time=time,
                              inputs=held[i], values=values))
        assert seen == {(i, step) for i in range(2) for step in range(STEPS)}
        write(out / f"{name}.sil.json", trace)
        results[name] = {"manifest_sha256": manifest_hash,
                         "recording_sha256": digest(recordings[0]),
                         "byte_deterministic": True, "checked_outputs": len(trace)}
    write(out / "results.json", results)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
