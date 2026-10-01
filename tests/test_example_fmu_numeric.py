"""The numeric-scalar FMU example at the run boundary (issue #189).

A CSV of Float32, Int32, UInt32 and UInt64 values is converted into a
Recording, authored into a Manifest with `sil-fmu-replay`, and replayed into
the Reference FMU `Feedthrough`, which copies each input to the output of the
same type. The outputs are compared with `reference.csv`, which states by hand
what each output holds one Step after its input — `16777217` as the Float32
`16777216` it rounds to, and every UInt64 above 2^53 to its last digit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ROOT, run_manifest

from sil import schema
from sil.compare import compare, read_contract
from sil.csv_recording import convert
from sil.fmi.authoring import author
from sil.recording import read_records

EXAMPLE = ROOT / "examples" / "fmu-numeric"
FEEDTHROUGH = (
    ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("fmu-numeric")
    convert(EXAMPLE / "mapping.json", EXAMPLE / "recorded.csv",
            workdir / "recorded.mcap")
    convert(EXAMPLE / "reference-mapping.json", EXAMPLE / "reference.csv",
            workdir / "reference.mcap")
    return workdir


def authored(prepared: Path, name: str) -> Path:
    out = prepared / f"{name}.json"
    author(EXAMPLE / "authoring.json", FEEDTHROUGH, prepared / "recorded.mcap",
           out)
    return out


def ran(sil_run: Path, manifest: Path, name: str) -> Path:
    proc = run_manifest(sil_run, manifest, manifest.with_name(f"{name}.mcap"))
    assert proc.returncode == 0, proc.stderr
    return proc.mcap_path


def test_authored_twice_and_run_twice_it_is_the_same_run(prepared, sil_run):
    first, second = authored(prepared, "first"), authored(prepared, "second")
    assert first.read_bytes() == second.read_bytes()
    assert ran(sil_run, first, "run-1").read_bytes() == (
        ran(sil_run, first, "run-2").read_bytes())


def test_the_outputs_agree_with_the_independent_reference(prepared, sil_run):
    manifest = authored(prepared, "nominal")
    report = compare(read_contract(EXAMPLE / "contract.json"),
                     ran(sil_run, manifest, "nominal"),
                     prepared / "reference.mcap")
    assert report["verdict"] == "pass", report
    assert report["channels"]["sensor.out"]["checked"] == 10


def test_the_recording_holds_the_values_above_2_53_exactly(prepared, sil_run):
    manifest = authored(prepared, "exact")
    recording = ran(sil_run, manifest, "exact")
    sensor = schema.load(
        json.loads(manifest.read_text())["schemas"])["numeric.Sensor"]
    published = [sensor.unpack(data)
                 for topic, _, data in read_records(recording)
                 if topic == "sensor.out"]
    assert [fields["sample_time_ns"] for fields in published[1:4]] == [
        2**64 - 1, 2**53 + 1, 2**63]
    # The Float32 values against IEEE 754 constants, not against `sil-csv`,
    # which converts both the input and the reference.
    assert [fields["range_m"] for fields in published[:4]] == [
        13421773 / 2**27, (2 - 2**-23) * 2**127, -(2 - 2**-23) * 2**127,
        2**-149]
    assert published[7]["range_m"] == 2**24


def test_the_receipt_records_each_type(prepared):
    receipt = author(EXAMPLE / "authoring.json", FEEDTHROUGH,
                     prepared / "recorded.mcap", prepared / "receipt.json")
    assert {binding["type"] for binding in receipt["bindings"]} == {
        "Float32", "Int32", "UInt32", "UInt64"}
