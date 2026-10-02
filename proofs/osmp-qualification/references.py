"""The independent references of the OSMP qualification (issue #233).

Nothing here reads an FMU output. Every expected value comes from the
closed-form source motion and the sensor geometry that upstream documents,
in `proofs/osmp-sensor/scene.py`, at the communication points the Importer
gives `fmi2DoStep`. Each reference is one CSV with its `sil-csv` mapping, so
the bundle's `sil-compare` contracts compare the decoded Recordings with it.

Three references, one per expected behavior:

- `nominal`: each sensor consumes the ground truth of the instant its own
  Step ends at.
- `late`: the prediction for a SensorView one Period late. The first Step
  has no input, so the sensor reports no valid output; every later Step
  reports the ground truth of the previous instant with the timestamp of its
  own (upstream takes the timestamp from the FMU time, not from the input).
- `unparseable`: a SensorView that does not parse. Upstream ignores the
  result of `ParseFromArray` and treats any non-empty input as valid, so the
  sensor reports valid output with no objects at every Step. Only a missing
  input (size 0) makes it report no valid output.
"""
import csv
import json
from pathlib import Path

import scene

STEPS = 1500
PERIOD_NS = scene.STEP_NS
DURATION_NS = STEPS * PERIOD_NS
VEHICLES = 10
# Every vehicle but the host can be detected.
DETECTIONS = VEHICLES - 1
SENSORS = {"sensor": 135.0, "sensor-50m": 50.0}
VIEW, TRUTH = "osi.SensorView", "osi.GroundTruth"
DETECTION_FLOATS = ("x", "y", "z", "yaw", "pitch", "roll",
                    "existence_probability", "length", "width", "height")
# Both sides compute in binary64 with the same formulas; the bound covers a
# different operation order and the last bits of libm's sin and cos. It is
# the bound of #230 and #244, not fitted to an observation.
FLOAT_RULE = {"atol": scene.TOL, "rtol": 0}

SCHEMAS = {
    "osi.GroundTruth": {"fields": [
        {"name": "seconds", "type": "i64"},
        {"name": "nanos", "type": "u32"},
        {"name": "host_id", "type": "u64"},
        {"name": "count", "type": "u32"},
        {"name": "id", "type": "u64", "count": VEHICLES},
        *({"name": axis, "type": "f64", "count": VEHICLES} for axis in "xyz"),
    ]},
    "osi.Detections": {"fields": [
        {"name": "seconds", "type": "i64"},
        {"name": "nanos", "type": "u32"},
        {"name": "count", "type": "u32"},
        {"name": "ground_truth_id", "type": "u64", "count": DETECTIONS},
        {"name": "tracking_id", "type": "u64", "count": DETECTIONS},
        *({"name": name, "type": "f64", "count": DETECTIONS}
          for name in DETECTION_FLOATS),
    ]},
    "sensor.Status": {"fields": [
        {"name": "valid", "type": "u8"},
        {"name": "count", "type": "i32"},
    ]},
}


def detections_channel(sensor: str) -> str:
    return f"{sensor}.Detections"


def status_channel(sensor: str) -> str:
    return f"{sensor}.Status"


def step_end_s(step: int) -> float:
    """The FMU time at the end of Step `step`, as the FMUs compute it.

    The Importer passes the communication point `t / 1e9` and the step
    `dt / 1e9`, each from integer nanoseconds, and both FMUs add the two
    doubles. The sum is not always `(t + dt) / 1e9`.
    """
    return step * PERIOD_NS / 1e9 + PERIOD_NS / 1e9


def _padded(values: list, count: int, zero) -> list:
    if len(values) > count:
        raise ValueError(f"{len(values)} values for {count} slots")
    return values + [zero] * (count - len(values))


def truth_fields(step: int) -> dict:
    now = step_end_s(step)
    seconds, nanos = scene.timestamp(now)
    vehicles = scene.ground_truth(now)
    return {"seconds": seconds, "nanos": nanos, "host_id": scene.HOST_ID,
            "count": len(vehicles), "id": [v["id"] for v in vehicles],
            **{axis: [v[axis] for v in vehicles] for axis in "xyz"}}


def detection_fields(seconds: int, nanos: int, found: list[dict]) -> dict:
    return {"seconds": seconds, "nanos": nanos, "count": len(found),
            **{name: _padded([d[name] for d in found], DETECTIONS, 0)
               for name in ("ground_truth_id", "tracking_id")},
            **{name: _padded([float(d[name]) for d in found], DETECTIONS, 0.0)
               for name in DETECTION_FLOATS}}


NO_OUTPUT = (detection_fields(0, 0, []), {"valid": 0, "count": 0})


def sensor_fields(step: int, nominal_range: float, kind: str) -> tuple[dict, dict]:
    """The decoded SensorData and the status of one sensor at one Step."""
    if kind == "late" and step == 0:
        return NO_OUTPUT
    seconds, nanos = scene.timestamp(step_end_s(step))
    if kind == "unparseable":
        return detection_fields(seconds, nanos, []), {"valid": 1, "count": 0}
    seen = step - 1 if kind == "late" else step
    found = scene.detections(scene.ground_truth(step_end_s(seen)),
                             scene.HOST_ID, nominal_range)
    return (detection_fields(seconds, nanos, found),
            {"valid": 1, "count": len(found)})


def channels(kind: str, sensors: dict[str, float]) -> dict[str, str]:
    """Each compared Channel of a reference and its schema."""
    # Only the nominal Run decodes the SensorView: a late SensorView Channel
    # would deliver it to the decoder late as well.
    named = {TRUTH: "osi.GroundTruth"} if kind == "nominal" else {}
    for sensor in sensors:
        named[detections_channel(sensor)] = "osi.Detections"
        named[status_channel(sensor)] = "sensor.Status"
    return named


def rows(kind: str, sensors: dict[str, float]):
    """One dict of Channel → fields per observation time."""
    for step in range(STEPS):
        row = {TRUTH: truth_fields(step)} if kind == "nominal" else {}
        for sensor, nominal_range in sensors.items():
            data, status = sensor_fields(step, nominal_range, kind)
            row[detections_channel(sensor)] = data
            row[status_channel(sensor)] = status
        yield (step + 1) * PERIOD_NS, row


def _columns(channel: str, schema: str) -> dict[str, dict]:
    columns = {}
    for field in SCHEMAS[schema]["fields"]:
        name = f"{channel}.{field['name']}"
        columns[field["name"]] = (
            {"columns": [f"{name}[{i}]" for i in range(field["count"])]}
            if "count" in field else {"column": name})
    return columns


def mapping(kind: str, sensors: dict[str, float]) -> dict:
    named = channels(kind, sensors)
    return {
        "sil_csv_mapping": 1,
        "timestamp": {"column": "time_ns", "unit": "ns"},
        "schemas": {schema: SCHEMAS[schema] for schema in sorted(set(named.values()))},
        "channels": [{"channel": channel, "schema": schema,
                      "fields": _columns(channel, schema)}
                     for channel, schema in named.items()],
    }


def _cells(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [repr(v) if isinstance(v, float) else str(v) for v in values]


def write_csv(path: Path, kind: str, sensors: dict[str, float]) -> None:
    columns = mapping(kind, sensors)["channels"]
    header = ["time_ns"]
    for entry in columns:
        for field in entry["fields"].values():
            header += field.get("columns", [field.get("column")])
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for time_ns, row in rows(kind, sensors):
            cells = [str(time_ns)]
            for entry in columns:
                fields = row[entry["channel"]]
                for name in entry["fields"]:
                    cells += _cells(fields[name])
            writer.writerow(cells)


def contract(kind: str, sensors: dict[str, float], *, actual_offset_ns: int = PERIOD_NS) -> dict:
    """Every Step from the first to the last, each field with its rule.

    The Importer publishes in the Slot at `t` what its FMU reached at
    `t + 20 ms`, and the decoder republishes it in the same Slot, so the
    actual offset is one Period. The last observation is the Duration.
    """
    observations = {"start_ns": PERIOD_NS, "stop_ns": DURATION_NS, "step_ns": PERIOD_NS}
    compared = {}
    for channel, schema in channels(kind, sensors).items():
        compared[channel] = {
            "actual_offset_ns": actual_offset_ns, "reference_offset_ns": 0,
            "observations": observations,
            "fields": {field["name"]: (FLOAT_RULE if field["type"] == "f64" else "exact")
                       for field in SCHEMAS[schema]["fields"]},
        }
    return {"sil_comparison": 1,
            "evaluation": {"from_ns": PERIOD_NS, "to_ns": DURATION_NS},
            "channels": compared}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
