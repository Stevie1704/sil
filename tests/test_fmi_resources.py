"""Resource-dependent external FMUs must work without an exporter rebuild."""
import hashlib
import json
import platform
import struct
from pathlib import Path

import pytest
from sil.fmi.archive import Extraction
from sil.manifest import Manifest

FIXTURES = Path(__file__).parent / "fixtures/pythonfmu3"
linux_fmu = pytest.mark.skipif(
    platform.system() != "Linux" or platform.machine() != "x86_64",
    reason="External fixture qualified on Linux x86-64",
)


@pytest.mark.parametrize("model", ["AccController", "AccPlant"])
def test_retained_external_archive_identity(model):
    identity = json.loads((FIXTURES / f"{model}.identity.json").read_text())
    assert hashlib.sha256((FIXTURES / f"{model}.fmu").read_bytes()).hexdigest() == identity["archive_sha256"]


def test_absent_resources_have_no_path(tmp_path):
    assert Extraction.resource_path(tmp_path) is None
    (tmp_path / "resources").mkdir()
    assert Extraction.resource_path(tmp_path) == tmp_path / "resources"


@linux_fmu
@pytest.mark.parametrize("model,field,starts,wanted", [
    ("AccController", "accel_mps2", ["gap_m=43.5", "relative_speed_mps=0", "ego_speed_mps=25"],
     [0.35, 0.35]),
    ("AccPlant", "ego_position_m", ["accel_mps2=1.5"], [2.5075, 5.03]),
])
def test_importer_loads_external_resources_at_run_boundary(run_sil, tmp_path, model, field, starts, wanted):
    from sil.recording import read_records

    manifest = Manifest(duration_ns=200_000_000)
    manifest.add_schemas({"Output": {"fields": [{"name": field, "type": "f64"}]}})
    manifest.add_channel("out", schema="Output")
    command = ["python3", "-m", "sil.fmi", str((FIXTURES / f"{model}.fmu").resolve()),
               "--bind", f"out:{field}={field}"]
    for value in starts:
        command.extend(["--start", value])
    manifest.add_process("external", command=command, step_period_ns=100_000_000, publishes=["out"])
    path = tmp_path / "manifest.json"
    manifest.write(path)
    result = run_sil(path)
    assert result.returncode == 0, result.stderr
    records = list(read_records(result.mcap_path))
    assert [t for _, t, _ in records] == [0, 100_000_000]
    assert [struct.unpack("<d", data)[0] for _, _, data in records] == pytest.approx(wanted)


@linux_fmu
def test_group_wires_the_archives_resource_directory(monkeypatch, tmp_path):
    """Group lifecycle policy is separate; verify its constructor handoff too."""
    from sil.fmi import ModelDescription
    from sil.fmi import terminals
    import zipfile

    with zipfile.ZipFile(FIXTURES / "AccController.fmu") as archive:
        archive.extractall(tmp_path)
    received = []

    class LifecycleProbe:
        def __init__(self, binary, description, *, event_mode, resource_path):
            received.append(resource_path)

        def apply_start_values(self, starts):
            pass

        def initialize(self):
            pass

    monkeypatch.setattr(terminals, "CoSimulation", LifecycleProbe)
    instance = terminals.Instance("resource-handoff", ModelDescription.read(tmp_path))
    instance.instantiate(tmp_path, [])
    assert received == [tmp_path / "resources"]
