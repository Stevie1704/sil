"""Closed-loop contracts, comparison failures and reproducible evidence export."""
import copy
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from closed_loop import check_kpi_coverage, execute
from loop_compare import compare, first_difference, recording_messages, validate_reference
from loop_contract import (FIELDS, INITIAL_OUTPUTS, PYTHON_SCHEDULE, STEP_NS, STEPS,
                           manifest, validate_archives, validate_manifest)
from loop_evidence import retain
from loop_kpi import MinimumGap
from proof_support import run_expecting, write_json
from sil.participant import Input

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/pythonfmu3"


def data():
    rows = {n * STEP_NS: {ch: [0.0] * len(names) for ch, names in FIELDS.items()}
            for n in range(STEPS)}
    reference = dict(grid=dict(step_ns=STEP_NS, steps=STEPS),
                     initialization=copy.deepcopy(INITIAL_OUTPUTS),
                     intervals=[dict(slot_ns=t, interval_end_ns=t + STEP_NS, **copy.deepcopy(row))
                                for t, row in rows.items()])
    return rows, reference


def test_identical():
    compare(*data())


@pytest.mark.parametrize("fault", ["final", "timestamp", "nan", "command", "extra", "width"])
def test_rejects(fault):
    rows, reference = data()
    if fault == "final":
        rows.pop((STEPS - 1) * STEP_NS)
    elif fault == "timestamp":
        reference["intervals"][2]["interval_end_ns"] += STEP_NS
    elif fault == "nan":
        rows[0]["sensing"][0] = float("nan")
    elif fault == "command":
        rows[STEP_NS]["command"][0] = 0.1
    elif fault == "width":
        reference["intervals"][0]["command"] = []
    else:
        rows[0]["unknown"] = [0]
    with pytest.raises(RuntimeError):
        compare(rows, reference)


@pytest.mark.parametrize("field", ["step_ns", "steps"])
def test_reference_grid_mismatch_rejected(field):
    _, reference = data()
    reference["grid"][field] *= 2
    with pytest.raises(RuntimeError, match="communication grid"):
        validate_reference(reference)


def test_initialization_is_asserted():
    _, reference = data()
    reference["initialization"]["command"] = [0]
    with pytest.raises(RuntimeError, match="accel_mps2 initialization"):
        validate_reference(reference)


def test_original_schedule_requires_absent_initial_command():
    rows, reference = data()
    for row in rows.values():
        del row["state"]
    del rows[0]["command"]
    reference["intervals"][0]["command"] = None
    compare(rows, reference, PYTHON_SCHEDULE)
    rows[0]["command"] = [0]
    with pytest.raises(RuntimeError, match="extra Channel"):
        compare(rows, reference, PYTHON_SCHEDULE)


def test_behavioral_difference_uses_declared_tolerance():
    nominal, _ = data()
    variant = copy.deepcopy(nominal)
    variant[0]["command"] = [1e-15]
    with pytest.raises(RuntimeError, match="did not change"):
        first_difference(nominal, variant, "command")
    variant[STEP_NS]["command"] = [0.01]
    assert first_difference(nominal, variant, "command")["publication_ns"] == STEP_NS


def test_qualified_archive_contract():
    validate_archives(FIXTURES)


@pytest.mark.parametrize("attribute,value", [("unit", "s"), ("causality", "output"),
                                             ("start", "nan"), ("name", "unknown")])
def test_invalid_archive_contract(tmp_path, attribute, value):
    with zipfile.ZipFile(FIXTURES / "AccController.fmu") as source:
        root = ET.fromstring(source.read("modelDescription.xml"))
    root.find("ModelVariables/Float64[@name='gap_m']").set(attribute, value)
    with zipfile.ZipFile(tmp_path / "AccController.fmu", "w") as target:
        target.writestr("modelDescription.xml", ET.tostring(root))
    with pytest.raises(RuntimeError, match="invalid"):
        validate_archives(tmp_path)


@pytest.mark.parametrize("records", [[("sensing", 0, bytes(24))] * 2,
                                     [("sensing", 1, bytes(24))],
                                     [("unknown", 0, bytes(24))]])
def test_recording_rejects_duplicate_mistimed_unknown(monkeypatch, records):
    monkeypatch.setattr("loop_compare.read_records", lambda path: records)
    with pytest.raises(RuntimeError):
        recording_messages("unused")


@pytest.mark.parametrize("fault", ["rate", "duration", "capacity"])
def test_manifest_configuration_checked(fault):
    doc = manifest().to_doc()
    if fault == "rate":
        doc["participants"]["controller"]["step_period_ns"] *= 2
    elif fault == "duration":
        doc["duration_ns"] += STEP_NS
    else:
        doc["participants"]["controller"]["subscribes"][0]["capacity"] = 0
    with pytest.raises(RuntimeError):
        validate_manifest(doc)


def test_manifest_is_independently_authored_twice(tmp_path):
    thresholds = iter((5.0, 6.0))
    with pytest.raises(RuntimeError, match="byte mismatch"):
        execute(lambda: manifest(minimum_gap_m=next(thresholds)), "unstable", tmp_path)


def test_expected_failure_does_not_accept_another_exit(tmp_path):
    args = [sys.executable, "-c", "raise SystemExit(2)"]
    run_expecting(args, tmp_path / "ok.log", 2)
    with pytest.raises(RuntimeError, match="expected 1"):
        run_expecting(args, tmp_path / "wrong.log", 1)


def test_kpi_rejects_unsafe_and_nonfinite_gap():
    kpi = MinimumGap(5, STEP_NS, STEPS)
    for gap in (4.0, float("nan")):
        message = Input(channel="sensing", publish_ns=0, data={"gap_m": gap})
        with pytest.raises(RuntimeError, match="minimum-gap KPI"):
            kpi.on_step(STEP_NS, STEP_NS, [message])


def test_kpi_receipt_requires_all_but_final_message(tmp_path):
    recording = tmp_path / "nominal.mcap"
    recording.with_suffix(".log").write_text('ACC_KPI {"checked_messages": 1, "last_publication_ns": 0}\n')
    with pytest.raises(RuntimeError, match="coverage"):
        check_kpi_coverage(recording)


def test_compact_report_is_reproducible_and_identifies_raw_evidence(tmp_path):
    source, first, second = [tmp_path / name for name in ("raw", "first", "second")]
    source.mkdir()
    for name in ("results", "environment", "configuration", "AccController.identity", "AccPlant.identity"):
        write_json(source / f"{name}.json", {"source": name})
    (source / "nominal.fmpy.json").write_text('{"command": null, "controller_output": [1.5]}')
    (source / "closed-loop-1.mcap").write_bytes(b"retained artifact")
    retain(source, first)
    retain(source, second)
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    report = json.loads((first / "report.json").read_text())
    assert report["fmus"]["AccPlant"] == {"source": "AccPlant.identity"}
    assert "nominal.fmpy.json" in report["artifacts_sha256"]
    digest = report["artifacts_sha256"]["closed-loop-1.mcap"]
    (source / "closed-loop-1.mcap").write_bytes(b"different artifact")
    retain(source, second)
    changed = json.loads((second / "report.json").read_text())
    assert changed["artifacts_sha256"]["closed-loop-1.mcap"] != digest
    assert not (first / "closed-loop-1.mcap").exists()
