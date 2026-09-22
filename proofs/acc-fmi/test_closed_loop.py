"""Comparator rejects coupling mistakes, missing final outputs and nonfinite data."""
import copy

import pytest
from closed_loop import FIELDS, STEP_NS, STEPS, compare


def data():
    rows = {n * STEP_NS: {ch: [0.0] * len(names) for ch, names in FIELDS.items()}
            for n in range(STEPS)}
    reference = [dict(slot_ns=t, communication_ns=t + STEP_NS, **copy.deepcopy(row))
                 for t, row in rows.items()]
    return rows, reference


def test_identical():
    compare(*data())


@pytest.mark.parametrize("fault", ["final", "timestamp", "nan", "command", "extra"])
def test_rejects(fault):
    rows, reference = data()
    if fault == "final":
        rows.pop((STEPS - 1) * STEP_NS)
    elif fault == "timestamp":
        reference[2]["communication_ns"] += STEP_NS
    elif fault == "nan":
        rows[0]["sensing"][0] = float("nan")
    elif fault == "command":
        rows[STEP_NS]["command"][0] = 0.1
    else:
        rows[0]["unknown"] = [0]
    with pytest.raises(RuntimeError):
        compare(rows, reference)


def test_qualified_archive_contract():
    from pathlib import Path
    from closed_loop import validate_archives
    validate_archives(Path(__file__).resolve().parents[2] / "tests/fixtures/pythonfmu3")


@pytest.mark.parametrize("attribute,value", [("unit", "s"), ("causality", "output"),
                                             ("start", "nan"), ("name", "unknown")])
def test_invalid_archive_contract(tmp_path, attribute, value):
    import zipfile
    from pathlib import Path
    from xml.etree import ElementTree as ET
    from closed_loop import validate_archives
    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/pythonfmu3/AccController.fmu"
    with zipfile.ZipFile(fixture) as source:
        root = ET.fromstring(source.read("modelDescription.xml"))
    variable = root.find("ModelVariables/Float64[@name='gap_m']")
    variable.set(attribute, value)
    with zipfile.ZipFile(tmp_path / "AccController.fmu", "w") as target:
        target.writestr("modelDescription.xml", ET.tostring(root))
    with pytest.raises(RuntimeError, match="invalid"):
        validate_archives(tmp_path)


@pytest.mark.parametrize("records", [[("sensing", 0, bytes(24))] * 2,
                                     [("sensing", 1, bytes(24))],
                                     [("unknown", 0, bytes(24))]])
def test_recording_rejects_duplicate_mistimed_unknown(monkeypatch, records):
    from closed_loop import recording_samples
    monkeypatch.setattr("closed_loop.read_records", lambda path: records)
    with pytest.raises(RuntimeError):
        recording_samples("unused")
