"""The independent expected slice for OSMPDummySensor fed by OSMPDummySource.

Ground truth is the source's documented closed-form motion: ten vehicles,
vehicle i at x = x0 + t * v and y = y0 + sin(t / v) * 0.25, with zero z and
zero orientation. Vehicle 14 is the host. The sensor geometry is the one
upstream documents in `OSMPDummySensor.cpp`: positions relative to the host's
rear axle and the sensor mounting, a range of 1.1 times the nominal range
(inclusive), a cone of about 30 degrees around the x axis (x / d > 0.866025)
and an existence probability of cos((2 d - range) / range).

The source sets no host vehicle attributes and no mounting position, so both
offsets are zero, and every orientation is zero, so the frame change is a
translation. `detections` refuses a scene outside that, instead of
silently omitting the rotation. Nothing here reads an FMU output.
"""
import math

STEP_NS = 20_000_000
STEP_S = STEP_NS / 1e9
HOST_ID = 14
SENSOR_ID = 10000
# OSMPDummySource.cpp, doCalc: one entry per vehicle, ids 10 to 19.
Y_OFFSETS_M = (3.0, 3.0, 3.0, 0.25, 0.0, -0.25, -3.0, -3.0, -3.0, -3.0)
X_OFFSETS_M = (0.0, 40.0, 100.0, 100.0, 0.0, 150.0, 5.0, 45.0, 85.0, 125.0)
SPEEDS_MPS = (29.0, 30.0, 31.0, 25.0, 26.0, 28.0, 20.0, 22.0, 22.5, 23.0)
LENGTH_M, WIDTH_M, HEIGHT_M = 5.0, 2.0, 1.5
RANGE_FACTOR = 1.1
COS_HALF_CONE = 0.866025
TOL = 1e-9
POSE = ("x", "y", "z", "yaw", "pitch", "roll")


def ground_truth(time):
    """The source's vehicles at simulation time `time` (the end of its step)."""
    return [{"id": 10 + i, "x": x0 + time * v, "y": y0 + math.sin(time / v) * 0.25, "z": 0.0,
             "yaw": 0.0, "pitch": 0.0, "roll": 0.0,
             "length": LENGTH_M, "width": WIDTH_M, "height": HEIGHT_M}
            for i, (x0, y0, v) in enumerate(zip(X_OFFSETS_M, Y_OFFSETS_M, SPEEDS_MPS))]


def detections(objects, host_id, nominal_range):
    """What the sensor reports, in input order, for one ground-truth frame."""
    host = next(o for o in objects if o["id"] == host_id)
    if any(o[axis] != 0.0 for o in objects for axis in ("yaw", "pitch", "roll")):
        raise ValueError("the expected slice covers translation only; an orientation is not zero")
    actual_range = nominal_range * RANGE_FACTOR
    found = []
    for vehicle in objects:
        if vehicle["id"] == host_id:
            continue
        rx, ry, rz = (vehicle[axis] - host[axis] for axis in ("x", "y", "z"))
        distance = math.sqrt(rx * rx + ry * ry + rz * rz)
        if distance <= actual_range and rx / distance > COS_HALF_CONE:
            found.append({
                "ground_truth_id": vehicle["id"], "tracking_id": len(found),
                "x": rx, "y": ry, "z": rz, "yaw": 0.0, "pitch": 0.0, "roll": 0.0,
                "existence_probability": math.cos((2.0 * distance - actual_range) / actual_range),
                "length": vehicle["length"], "width": vehicle["width"],
                "height": vehicle["height"]})
    return found


def timestamp(time):
    """OSI seconds and nanoseconds as both FMUs derive them: truncated."""
    seconds = math.floor(time)
    return seconds, int((time - seconds) * 1_000_000_000.0)


def _numeric_divergence(index, step, position, want, got):
    for field, value in want.items():
        other = got.get(field)
        same = (other == value if isinstance(value, int)
                else other is not None and abs(other - value) <= TOL)
        if not same:
            return {"step": index, "t_ns": step["t_ns"], "object": position, "field": field,
                    "expected": value, "actual": other}
    return None


def first_divergence(expected, actual):
    """The first step, object and field where `actual` leaves the expected slice."""
    for index, (want, got) in enumerate(zip(expected, actual)):
        for field in ("t_ns", "seconds", "nanos"):
            if want[field] != got[field]:
                return {"step": index, "t_ns": want["t_ns"], "field": field,
                        "expected": want[field], "actual": got[field]}
        ids = [[d["ground_truth_id"] for d in step["detections"]] for step in (want, got)]
        if ids[0] != ids[1]:
            return {"step": index, "t_ns": want["t_ns"], "field": "ground_truth_ids",
                    "expected": ids[0], "actual": ids[1]}
        for position, pair in enumerate(zip(want["detections"], got["detections"])):
            divergence = _numeric_divergence(index, want, position, *pair)
            if divergence:
                return divergence
    if len(expected) != len(actual):
        return {"step": min(len(expected), len(actual)), "field": "coverage",
                "expected": len(expected), "actual": len(actual)}
    return None
