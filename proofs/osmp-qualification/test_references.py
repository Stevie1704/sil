"""The references convert with sil-csv and predict the late control's divergence."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "osmp-sensor"))

import references  # noqa: E402
import scene  # noqa: E402
from sil.compare import compare, read_contract  # noqa: E402
from sil.csv_recording import convert  # noqa: E402

SENSORS = references.SENSORS


def row(kind, step):
    return list(references.rows(kind, SENSORS))[step][1]


def test_nominal_is_the_ground_truth_of_the_step_end():
    time_ns, fields = list(references.rows("nominal", SENSORS))[0]
    found = scene.detections(scene.ground_truth(0.02), scene.HOST_ID, 135.0)
    assert time_ns == 20_000_000
    assert fields["sensor.Status"] == {"valid": 1, "count": len(found)}
    assert fields["sensor.Detections"]["nanos"] == 20_000_000
    assert fields["sensor.Detections"]["x"][:len(found)] == [d["x"] for d in found]
    assert fields["osi.GroundTruth"]["id"] == list(range(10, 20))


def test_the_range_parameter_changes_the_slice():
    counts = [(f["sensor.Status"]["count"], f["sensor-50m.Status"]["count"])
              for _, f in references.rows("nominal", SENSORS)]
    assert all(near <= far for far, near in counts)
    assert any(near < far for far, near in counts)


def test_late_reports_nothing_first_then_the_previous_instant():
    first, later = row("late", 0), row("late", 5)
    assert first["sensor.Status"] == {"valid": 0, "count": 0}
    assert first["sensor.Detections"]["nanos"] == 0
    previous = scene.detections(scene.ground_truth(references.step_end_s(4)),
                                scene.HOST_ID, 135.0)
    assert later["sensor.Detections"]["x"][:len(previous)] == [d["x"] for d in previous]
    assert later["sensor.Detections"]["nanos"] == row("nominal", 5)["sensor.Detections"]["nanos"]
    assert "osi.GroundTruth" not in first


def test_unparseable_reports_valid_output_without_objects():
    nominal = references.rows("nominal", SENSORS)
    unparseable = references.rows("unparseable", {"sensor": 135.0})
    for (_, want), (_, fields) in zip(nominal, unparseable, strict=True):
        assert fields["sensor.Status"] == {"valid": 1, "count": 0}
        assert fields["sensor.Detections"]["nanos"] == want["sensor.Detections"]["nanos"]


def _recording(tmp_path, kind):
    csv, mapping = tmp_path / f"{kind}.csv", tmp_path / f"{kind}.mapping.json"
    references.write_csv(csv, kind, SENSORS)
    references.write_json(mapping, references.mapping(kind, SENSORS))
    recording = tmp_path / f"{kind}.mcap"
    receipt = convert(mapping, csv, recording)
    assert receipt["channels"]
    return recording


def test_late_prediction_first_leaves_the_nominal_reference_at_the_first_step(tmp_path):
    contract = tmp_path / "contract.json"
    references.write_json(contract, references.contract("late", SENSORS, actual_offset_ns=0))
    rules = read_contract(contract)
    late, nominal = _recording(tmp_path, "late"), _recording(tmp_path, "nominal")
    assert compare(rules, nominal, nominal)["verdict"] == "pass"
    report = compare(rules, late, nominal)
    first = report["first_divergence"]
    assert report["verdict"] == "fail"
    assert (first["channel"], first["field"], first["observation_ns"]) == (
        "sensor.Detections", "nanos", 20_000_000)
    assert (first["actual"], first["expected"]) == (0, 20_000_000)


def test_initialization_leaves_every_output_empty():
    expected = references.initial_outputs(SENSORS)
    assert set(expected) == {"source", *SENSORS}
    for sensor in SENSORS:
        assert expected[sensor][f"{sensor}.Status"] == {"valid": 0, "count": 0}
        assert expected[sensor][f"{sensor}.ConfigRequest"]["payload_length"] == 0
