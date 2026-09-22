"""Tests for the communication-interval experiment's public evidence seams."""
import copy

import pytest

from sensitivity_compare import analytic_state, compare_observations, within_envelope
from sensitivity_contract import configuration, measured_rows, reference_rows
from sensitivity_manifest import manifest_for, route_capacity


def test_matrix_declares_the_three_rate_factors_and_their_separation():
    document = configuration()
    rows = measured_rows()
    names = {row["name"] for row in rows}

    assert {"combined-20ms", "combined-10ms", "combined-5ms"} <= names
    assert {"sampling-20ms", "sampling-10ms", "sampling-5ms"} <= names
    assert {"latency-0ms", "latency-10ms", "latency-20ms"} <= names
    assert document["observation_grid_ns"] == 1_000_000
    assert document["comparison_tolerances"]["absolute_si"] == 1e-9
    assert document["timing_contract"]["can_handle_variable_step_size"] is False
    assert document["limitations"]["fixed_controller_sampling_in_numeric_refinement"] is False
    assert all("periods_ns" in row and "latencies_ns" in row for row in rows)
    assert {row["name"] for row in reference_rows()} >= {
        "fine-combined-1ms", "fine-sampling-1ms", "fine-latency-0ms",
        "fine-latency-10ms", "fine-latency-20ms", "fine-timing-defect",
    }


def test_constant_acceleration_oracle_is_analytic_and_independent():
    state = analytic_state(1_000_000_000, 1.5)

    assert state == {
        "ego_position_m": 25.75,
        "ego_speed_mps": 26.5,
        "lead_position_m": 85.0,
        "lead_speed_mps": 25.0,
        "gap_m": 59.25,
        "relative_speed_mps": -1.5,
    }


def test_comparison_requires_an_exact_reference_time_not_interpolation():
    measured = {"state": {20_000_000: [1.0]}}
    reference = {"state": {19_000_000: [0.0], 21_000_000: [2.0]}}

    with pytest.raises(RuntimeError, match="exact observation time"):
        compare_observations(measured, reference, {"state": ["value"]})


def test_timing_defect_is_outside_the_declared_envelope():
    normal = {"max_abs_error": {"gap_m": 0.25, "ego_speed_mps": 0.08,
                                "accel_mps2": 0.09}}
    defect = copy.deepcopy(normal)
    defect["max_abs_error"]["accel_mps2"] = 0.11

    assert within_envelope(normal)
    assert not within_envelope(defect)


def test_constant_acceleration_manifest_has_only_the_plant_exchange():
    row = next(item for item in measured_rows() if item["name"] == "constant-20ms")

    document = manifest_for(row).to_doc()

    assert set(document["participants"]) == {"plant"}
    assert document["participants"]["plant"]["step_period_ns"] == 20_000_000
    assert document["participants"]["plant"]["publishes"] == ["state"]
    assert document["channels"]["state"]["latency_ns"] == 0
    assert "state:gap_m=gap_m" in document["participants"]["plant"]["command"]


def test_sampling_manifest_keeps_plant_and_channel_latency_fixed():
    row = next(item for item in measured_rows() if item["name"] == "sampling-5ms")

    document = manifest_for(row).to_doc()

    assert document["participants"]["plant"]["step_period_ns"] == 10_000_000
    assert document["participants"]["controller"]["step_period_ns"] == 5_000_000
    assert document["channels"]["sensing"]["latency_ns"] == 10_000_000
    assert document["channels"]["command"]["latency_ns"] == 10_000_000
    assert "command:accel_mps2=accel_mps2" in document["participants"]["plant"]["command"]
    assert document["participants"]["controller"]["subscribes"][0]["capacity"] == route_capacity(
        10_000_000, 5_000_000, 10_000_000
    )
