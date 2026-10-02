"""The independent expected slice: closed-form source motion and sensor geometry."""
import math

import pytest
from scene import HOST_ID, detections, first_divergence, ground_truth, timestamp

RANGE_M = 135.0 * 1.1


def vehicle(identifier, x, y=0.0):
    return {"id": identifier, "x": x, "y": y, "z": 0.0, "yaw": 0.0, "pitch": 0.0, "roll": 0.0,
            "length": 5.0, "width": 2.0, "height": 1.5}


def test_the_source_moves_each_vehicle_in_closed_form():
    objects = {o["id"]: o for o in ground_truth(2.0)}
    assert sorted(objects) == list(range(10, 20))
    host = objects[HOST_ID]
    assert host["x"] == 0.0 + 2.0 * 26.0
    assert host["y"] == 0.0 + math.sin(2.0 / 26.0) * 0.25
    assert objects[15]["x"] == 150.0 + 2.0 * 28.0
    assert objects[15]["y"] == -0.25 + math.sin(2.0 / 28.0) * 0.25
    assert all((o["z"], o["yaw"], o["length"]) == (0.0, 0.0, 5.0) for o in objects.values())


def test_a_vehicle_ahead_is_reported_relative_to_the_host():
    found = detections([vehicle(1, 100.0, 1.0), vehicle(2, 110.0, 2.0)], 1, 135.0)
    assert len(found) == 1
    assert found[0]["ground_truth_id"] == 2 and found[0]["tracking_id"] == 0
    assert (found[0]["x"], found[0]["y"], found[0]["z"]) == (10.0, 1.0, 0.0)
    distance = math.sqrt(101.0)
    assert found[0]["existence_probability"] == math.cos((2.0 * distance - RANGE_M) / RANGE_M)


@pytest.mark.parametrize("x, y, seen", [
    (RANGE_M, 0.0, True),          # the range boundary is inclusive
    (RANGE_M + 1e-9, 0.0, False),
    (10.0, 5.0, True),             # inside the 30 degree cone
    (10.0, 6.0, False),            # outside it
    (-10.0, 0.0, False),           # behind the host
])
def test_range_and_cone_decide_what_is_seen(x, y, seen):
    assert bool(detections([vehicle(1, 0.0), vehicle(2, x, y)], 1, 135.0)) is seen


def test_tracking_ids_count_only_the_reported_vehicles_in_input_order():
    objects = [vehicle(5, 30.0), vehicle(1, 0.0), vehicle(6, -30.0), vehicle(7, 20.0)]
    found = detections(objects, 1, 135.0)
    assert [(d["ground_truth_id"], d["tracking_id"]) for d in found] == [(5, 0), (7, 1)]


def test_the_nominal_range_parameter_scales_the_range():
    objects = [vehicle(1, 0.0), vehicle(2, 120.0)]
    assert detections(objects, 1, 135.0) and not detections(objects, 1, 100.0)


def test_timestamps_truncate_to_whole_nanoseconds():
    assert timestamp(1.5) == (1, 500_000_000)
    assert timestamp(0.02) == (0, 20_000_000)


def step(*detected):
    return {"t_ns": 20_000_000, "seconds": 0, "nanos": 20_000_000, "detections": list(detected)}


def detected(identifier, x):
    return {"ground_truth_id": identifier, "tracking_id": 0, "x": x, "y": 0.0, "z": 0.0,
            "existence_probability": 0.5}


def test_agreement_inside_tolerance_is_no_divergence():
    assert first_divergence([step(detected(2, 10.0))], [step(detected(2, 10.0 + 1e-12))]) is None


def test_the_first_divergence_names_step_object_field_and_values():
    divergence = first_divergence([step(detected(2, 10.0))], [step(detected(2, 10.5))])
    assert divergence == {"step": 0, "t_ns": 20_000_000, "object": 0, "field": "x",
                          "expected": 10.0, "actual": 10.5}


def test_a_different_set_of_reported_vehicles_diverges():
    divergence = first_divergence([step(detected(2, 10.0))], [step()])
    assert divergence["field"] == "ground_truth_ids"
    assert (divergence["expected"], divergence["actual"]) == ([2], [])


def test_a_missing_step_diverges():
    assert first_divergence([step(), step()], [step()])["field"] == "coverage"
