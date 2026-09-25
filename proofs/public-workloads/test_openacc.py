"""Recording inspection and conversion policies, on small synthetic recordings."""
import pytest
from openacc import Window, controller_inputs, inspect, lead_acceleration, parse

HEADER = """Date,4,7,2019,,
Vehicle_order,Lead(A),Ego(B),,,
Number_of_vehicles,2,,,,
ACC,1,,,,
Distance_setting,min,,,,
Time,Speed1,Speed2,Driver1,Driver2,IVS1
"""


def recording(rows):
    return parse(HEADER + "".join(f"{t},{v1},{v2},ACC,ACC,{gap}\n" for t, v1, v2, gap in rows))


STEADY = recording([(round(1.0 + k / 10, 1), 20.0 + k / 10, 19.5, 30.0 - k / 10) for k in range(11)])


def test_metadata_and_grid_are_reported():
    report = inspect(STEADY)
    assert report["metadata"]["Vehicle_order"] == ["Lead(A)", "Ego(B)"]
    assert report["samples"] == 11
    assert report["first_time_s"] == 1.0 and report["last_time_s"] == 2.0
    assert report["period_ns"] == {"100000000": 10}
    assert report["gaps"] == [] and report["missing"] == {}


def test_a_gap_and_a_missing_value_are_found():
    broken = recording([(1.0, 20, 19, 30), (1.1, 20, 19, 30), (1.4, 20, "", 30)])
    report = inspect(broken)
    assert report["gaps"] == [{"after_s": 1.1, "interval_ns": 300_000_000}]
    assert report["missing"] == {"Speed2": 1}


def test_window_rebases_time_exactly_and_maps_the_follower():
    window = Window(start_s=1.2, end_s=1.5, lead=1, ego=2)
    inputs = controller_inputs(STEADY, window)
    assert [row["t_ns"] for row in inputs] == [0, 100_000_000, 200_000_000, 300_000_000]
    first = inputs[0]
    assert first["gap_m"] == pytest.approx(29.8)
    assert first["relative_speed_mps"] == pytest.approx(20.2 - 19.5)
    assert first["ego_speed_mps"] == 19.5


def test_a_window_outside_the_recording_is_rejected():
    with pytest.raises(ValueError, match="window"):
        controller_inputs(STEADY, Window(start_s=1.5, end_s=2.5, lead=1, ego=2))


def test_a_window_across_a_gap_is_rejected():
    broken = recording([(1.0, 20, 19, 30), (1.1, 20, 19, 30), (1.4, 20, 19, 30)])
    with pytest.raises(ValueError, match="grid"):
        controller_inputs(broken, Window(start_s=1.0, end_s=1.4, lead=1, ego=2))


def test_held_forward_difference_reconstructs_every_recorded_lead_speed():
    window = Window(start_s=1.0, end_s=2.0, lead=1, ego=2)
    accelerations = lead_acceleration(STEADY, window)
    assert len(accelerations) == 10
    assert all(a == pytest.approx(1.0) for a in accelerations)
    recorded = [row["Speed1"] for row in STEADY["rows"]]
    rebuilt = [recorded[0]]
    for acceleration in accelerations:
        rebuilt.append(rebuilt[-1] + acceleration * 0.1)
    assert max(abs(a - b) for a, b in zip(rebuilt, recorded)) < 1e-12
