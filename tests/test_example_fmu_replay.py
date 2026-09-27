"""The recorded-data FMU example at the run boundary (issue #186).

A CSV of acceleration in cm/s² is converted into m/s² at the edge, authored
into a Manifest with `sil-fmu-replay`, and replayed into `EgoMotion`, which
integrates it from the parameters' initial speed and position. The outputs
are compared with `reference.csv`, which states the closed-form trajectory
and was computed without running the FMU.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from conftest import ROOT, run_manifest

from sil import schema
from sil.compare import compare, read_contract
from sil.csv_recording import convert
from sil.fmi.authoring import author
from sil.recording import read_records

EXAMPLE = ROOT / "examples" / "fmu-replay"
MS = 1_000_000


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packaging = _load("fmu_replay_package", EXAMPLE / "package.py")


@pytest.fixture(scope="module")
def prepared(build_dir: Path, tmp_path_factory) -> Path:
    """The FMU, the converted input and the converted reference."""
    workdir = tmp_path_factory.mktemp("fmu-replay")
    binary = build_dir / "EgoMotion.so"
    assert binary.is_file(), f"example FMU binary missing at {binary}"
    packaging.package(binary, workdir / "EgoMotion.fmu")
    convert(EXAMPLE / "mapping.json", EXAMPLE / "recorded.csv",
            workdir / "recorded.mcap")
    convert(EXAMPLE / "reference-mapping.json", EXAMPLE / "reference.csv",
            workdir / "reference.mcap")
    return workdir


def authored(prepared: Path, name: str, document: Path | None = None) -> Path:
    out = prepared / f"{name}.json"
    author(document or EXAMPLE / "authoring.json",
           prepared / "EgoMotion.fmu", prepared / "recorded.mcap", out)
    return out


def ran(sil_run: Path, manifest: Path, name: str) -> Path:
    proc = run_manifest(sil_run, manifest, manifest.with_name(f"{name}.mcap"))
    assert proc.returncode == 0, proc.stderr
    return proc.mcap_path


def compared(recording: Path, prepared: Path) -> dict:
    return compare(read_contract(EXAMPLE / "contract.json"), recording,
                   prepared / "reference.mcap")


def motion(recording: Path, manifest: Path) -> list[tuple[int, dict]]:
    schemas = schema.load(json.loads(manifest.read_text())["schemas"])
    return [(t, schemas["ego.Motion"].unpack(data))
            for topic, t, data in read_records(recording)
            if topic == "ego.motion"]


def test_authored_twice_and_run_twice_it_is_the_same_run(prepared, sil_run):
    first, second = authored(prepared, "first"), authored(prepared, "second")
    assert first.read_bytes() == second.read_bytes()
    assert ran(sil_run, first, "run-1").read_bytes() == (
        ran(sil_run, first, "run-2").read_bytes())


def test_the_outputs_agree_with_the_independent_reference(prepared, sil_run):
    manifest = authored(prepared, "nominal")
    report = compared(ran(sil_run, manifest, "nominal"), prepared)
    assert report["verdict"] == "pass", report
    assert report["channels"]["ego.motion"]["checked"] == 10


def test_the_first_step_sees_the_parameters_and_the_input_at_zero(
    prepared, sil_run
):
    manifest = authored(prepared, "first-step")
    t, first = motion(ran(sil_run, manifest, "first-step"), manifest)[0]
    # Published at 0, the values at 10 ms: 20 m/s and 5 m from the
    # parameters, driven by the 1.5 m/s² recorded at 0.
    assert t == 0
    assert first == pytest.approx(
        {"speed_mps": 20.015, "position_m": 5.200075}, abs=1e-12)


def test_one_period_of_input_latency_is_a_different_run(
    prepared, sil_run, tmp_path
):
    """Under a Latency of one Period, the input recorded at 0 reaches the
    FMU at 10 ms: its first Step holds the input's declared start of 0."""
    document = json.loads((EXAMPLE / "authoring.json").read_text())
    document["channels"]["ego.accel"]["latency_ns"] = 10 * MS
    late = tmp_path / "late.json"
    late.write_text(json.dumps(document))
    manifest = authored(prepared, "late", late)
    recording = ran(sil_run, manifest, "late")
    assert motion(recording, manifest)[0][1]["speed_mps"] == 20.0
    report = compared(recording, prepared)
    assert report["verdict"] == "fail"


def test_the_provenance_digests_the_fmu(prepared, sil_run):
    manifest = authored(prepared, "provenance")
    recording = ran(sil_run, manifest, "provenance")
    sidecar = json.loads(
        recording.with_name(recording.name + ".provenance.json").read_text())
    files = {entry["file"]["path"]: entry["file"]["sha256"]
             for entry in sidecar["artifacts"]["command_files"]
             if entry["file"]}
    fmu = (prepared / "EgoMotion.fmu").resolve()
    receipt = author(EXAMPLE / "authoring.json", fmu,
                     prepared / "recorded.mcap", prepared / "again.json")
    assert files == {str(fmu): receipt["fmu"]["sha256"]}
