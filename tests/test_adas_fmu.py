"""The ADAS reference FMU export, without its preparation image (issue #225).

The proof in `proofs/adas-fmu/` builds the archive on Linux x86-64 in a
pinned image and drives it with FMPy. These tests run what needs neither:
the packaging is byte-reproducible on this host, the archive declares the
profile's interface, SiL's current inspection refuses the recorded-input
mapping only for missing types and arrays, and the committed pin still
names the sources in this checkout.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import ROOT, load_module

FMU_DIR = ROOT / "examples" / "adas-reference" / "fmu"
PROOF_DIR = ROOT / "proofs" / "adas-fmu"
PIN = PROOF_DIR / "evidence" / "AdasReference.identity.json"

package = load_module("adas_fmu_package", FMU_DIR / "package.py")
audit = load_module("adas_fmu_audit", PROOF_DIR / "audit.py")

pytestmark = pytest.mark.skipif(shutil.which("cc") is None,
                                reason="needs a C compiler")


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("adas-fmu")
    return out / "AdasReference.fmu", package.build(out)


def test_packaging_twice_writes_the_same_bytes(built, tmp_path):
    fmu, identity = built
    again = package.build(tmp_path)
    assert again == identity
    assert (tmp_path / "AdasReference.fmu").read_bytes() == fmu.read_bytes()


def test_a_build_variant_is_another_archive(built, tmp_path):
    _, identity = built
    control = package.build(tmp_path, defines=("ADAS_REFERENCE_WRONG_SIGN",))
    assert control["instantiation_token"] != identity["instantiation_token"]


def test_the_archive_declares_the_profile_interface(built):
    fmu, identity = built
    interface = audit.read_interface(fmu)
    assert audit.interface_findings(interface) == []
    assert interface["model"]["instantiationToken"] == \
        identity["instantiation_token"]


def test_a_changed_declaration_is_a_finding(built):
    fmu, _ = built
    interface = audit.read_interface(fmu)
    interface["variables"]["radar.x_m"]["dimensions"] = [9]
    interface["co_simulation"]["canHandleVariableCommunicationStepSize"] = \
        "true"
    del interface["variables"]["ego.speed_mps"]
    assert audit.interface_findings(interface) == [
        "CoSimulation " + str(interface["co_simulation"]),
        "variables missing ['ego.speed_mps'], unexpected []",
        "radar.x_m: dimensions is [9], expected [8]",
    ]


def test_the_recorded_input_mapping_binds_every_schema_field():
    mapping = audit.MAPPING
    assert mapping["bind"] == [
        f"{channel}:{field['name']}={channel}.{field['name']}"
        for channel, spec in mapping["channels"].items()
        for field in mapping["schemas"][spec["schema"]]["fields"]
    ]
    assert mapping["schemas"] == json.loads(
        (ROOT / "examples" / "adas-reference" / "schemas.json").read_text())


def test_inspection_refuses_only_missing_types_and_arrays(built):
    fmu, _ = built
    inspected = audit.inspection(fmu)
    assert audit.inspection_findings(inspected) == []
    assert inspected["missing_types"] == [
        "Float32", "Int32", "Int64", "UInt32", "UInt64", "UInt8"]
    assert inspected["arrays_refused"] == sorted(
        f"{sensor}:{field}={sensor}.{field}"
        for sensor in ("radar", "camera")
        for field in ("object_id", "x_m", "y_m", "relative_vx_mps",
                      "confidence"))


@pytest.mark.skipif(not PIN.exists(), reason="no archive is pinned yet")
def test_the_pin_names_this_checkout_s_sources():
    pinned = json.loads(PIN.read_text())
    current = package.identity("cc")
    for key in ("profile", "profile_version", "model_identifier", "flags",
                "defines", "sources", "licenses"):
        assert pinned[key] == current[key], (
            f"{key} changed since the archive was pinned; rerun "
            f"proofs/adas-fmu/run-proof.sh and commit its evidence")
    assert pinned["platform"] == "x86_64-linux"
