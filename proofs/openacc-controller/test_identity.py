"""The FMU's identity and declared interface, against what #178 audited."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from identity import archive_identity, audited_interface, inspected_interface

from sil.fmi.inspection import inspect

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "pythonfmu3" / "AccController.fmu"
AUDIT = ROOT / "proofs" / "public-workloads" / "evidence" / "fmu-audit.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_the_identity_names_the_model_source_the_archive_carries():
    identity = archive_identity(FIXTURE)
    with zipfile.ZipFile(FIXTURE) as archive:
        embedded = json.loads(archive.read("resources/identity.json"))
        controller = archive.read("resources/controller.py")
        dynamics = archive.read("resources/dynamics.py")
    assert identity == {
        "archive_sha256": sha256(FIXTURE.read_bytes()),
        "embedded": embedded,
        "model_sha256": sha256(controller),
        "dynamics_sha256": sha256(dynamics),
    }


def test_an_archive_whose_source_departs_from_its_identity_is_refused(tmp_path):
    changed = tmp_path / "AccController.fmu"
    with zipfile.ZipFile(FIXTURE) as source, \
            zipfile.ZipFile(changed, "w") as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "resources/dynamics.py":
                data += b"# changed\n"
            target.writestr(item, data)
    with pytest.raises(ValueError, match="dynamics.py"):
        archive_identity(changed)


def test_the_inspected_interface_is_the_one_178_audited():
    audited = audited_interface(json.loads(AUDIT.read_text())["AccController"])
    inspected = inspected_interface(inspect(FIXTURE))
    assert inspected == audited
    assert [v["name"] for v in inspected["variables"]] == [
        "time", "gap_m", "relative_speed_mps", "ego_speed_mps", "accel_mps2"]


def test_a_changed_declaration_is_a_different_interface(tmp_path):
    audit = json.loads(AUDIT.read_text())["AccController"]
    audit["variables"][1]["unit"] = "cm"
    shutil.copy(FIXTURE, tmp_path / "a.fmu")
    assert inspected_interface(inspect(tmp_path / "a.fmu")) != (
        audited_interface(audit))
