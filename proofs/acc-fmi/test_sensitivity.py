"""Tests for the public seams of the communication-period experiment."""
from __future__ import annotations

import copy

import pytest

from sensitivity_compare import (
    analytic_forcing_state,
    analytic_state,
    compare_observations,
    within_envelope,
)
from sensitivity_contract import (
    ACCEPTANCE_ENVELOPE,
    MANEUVER_PERIOD_NS,
    PERIODS_NS,
    configuration,
    lead_acceleration_at_ns,
    measured_rows,
    reference_rows,
)
from sensitivity_evidence import write_plots
from sensitivity_manifest import manifest_for, route_capacity
from sensitivity_report import markdown_table


def _row(name: str):
    return next(item for item in measured_rows() if item.name == name)


def test_matrix_separates_plant_period_controller_period_and_latency():
    document = configuration()
    names = {row.name for row in measured_rows()}

    assert {"plant-20ms", "plant-10ms", "plant-5ms"} <= names
    assert {"forcing-20ms", "forcing-10ms", "forcing-5ms"} <= names
    assert {"sampling-20ms", "sampling-10ms", "sampling-5ms"} <= names
    assert {"latency-0ms", "latency-10ms", "latency-20ms"} <= names
    assert {"combined-20ms", "combined-10ms", "combined-5ms"} <= names
    assert tuple(document["periods_ns"]) == PERIODS_NS
    assert document["maneuver"]["period_ns"] == MANEUVER_PERIOD_NS
    assert document["timing_contract"]["can_handle_variable_step_size"] is False

    plant_row = _row("plant-20ms")
    assert plant_row.periods["controller"] == 10_000_000
    assert plant_row.latencies["sensing"] == 10_000_000
    assert plant_row.latencies["command"] == 10_000_000
    assert plant_row.periods["maneuver"] == MANEUVER_PERIOD_NS
    assert all("periods_ns" in row.to_document() for row in measured_rows())
    assert {row.name for row in reference_rows()} >= {
        "fine-plant-1ms", "fine-sampling-1ms", "fine-latency-0ms",
        "fine-latency-10ms", "fine-latency-20ms", "fine-combined-1ms",
        "fine-timing-defect",
    }
    assert document["acceptance_envelope_basis"]["normal_period_limit_ns"] == max(PERIODS_NS)
    assert _row("timing-defect").latencies["sensing"] == 250_000_000


def test_configuration_authors_exchange_holds_and_envelope_basis():
    document = configuration()
    exchange = document["exchange_algorithm"]
    holds = document["sample_hold_rules"]
    basis = document["acceptance_envelope_basis"]

    assert exchange["applies_to"] == "all measured and independent-reference rows"
    assert "Manifest priority order" in " ".join(exchange["steps"])
    assert set(holds) == {
        "applies_to", "maneuver", "command", "sensing", "observation",
    }
    assert basis["one_period_full_span_speed_change_mps"] == pytest.approx(0.09)
    assert basis["position_budget_fraction_of_minimum_gap_threshold"] == 0.2
    assert basis["command_budget_fraction_of_full_span"] == pytest.approx(1 / 9)
    assert "fivefold allowance" in basis["policy"]


def test_maneuver_has_off_grid_changes_and_is_not_constant():
    values = [lead_acceleration_at_ns(time_ns) for time_ns in (
        0, 237_000_000, 613_000_000, 1_087_000_000, 1_463_000_000,
    )]

    assert values == [0.0, 1.5, -1.5, 0.75, -0.75]
    assert len(set(values)) > 1


def test_constant_acceleration_oracle_is_analytic_and_independent():
    state = analytic_state(1_000_000_000, 1.5, initial_lead_position_m=44.0)

    assert state == {
        "ego_position_m": 25.75,
        "ego_speed_mps": 26.5,
        "lead_position_m": 69.0,
        "lead_speed_mps": 25.0,
        "gap_m": 43.25,
        "relative_speed_mps": -1.5,
    }


def test_forcing_oracle_integrates_the_piecewise_lead_profile():
    state = analytic_forcing_state(
        1_000_000_000, initial_lead_position_m=44.0,
    )

    assert state["ego_position_m"] == 25.0
    assert state["ego_speed_mps"] == 25.0
    assert state["lead_speed_mps"] == pytest.approx(24.9835)
    assert state["lead_position_m"] == pytest.approx(69.21197325)
    assert state["gap_m"] == pytest.approx(44.21197325)


def test_comparison_requires_an_exact_reference_time_not_interpolation():
    measured = {"state": {20_000_000: [1.0]}}
    reference = {"state": {19_000_000: [0.0], 21_000_000: [2.0]}}

    with pytest.raises(RuntimeError, match="exact observation time"):
        compare_observations(measured, reference, {"state": ["value"]})


def test_declared_envelope_is_checked_against_observed_sensitivity():
    assert ACCEPTANCE_ENVELOPE["accel_mps2"] == 0.5
    normal = {"max_abs_error": {"gap_m": 0.49, "ego_speed_mps": 0.24,
                                "accel_mps2": 0.49}}
    defect = copy.deepcopy(normal)
    defect["max_abs_error"]["accel_mps2"] = 0.51

    assert within_envelope(normal)
    assert not within_envelope(defect)


def test_constant_acceleration_manifest_has_only_the_plant_exchange():
    document = manifest_for(_row("constant-20ms")).to_doc()

    assert set(document["participants"]) == {"plant"}
    assert document["participants"]["plant"]["step_period_ns"] == 20_000_000
    assert document["participants"]["plant"]["publishes"] == ["state"]
    assert "initial_lead_position_m=44.0" in document["participants"]["plant"]["command"]


def test_forcing_manifest_publishes_the_authored_maneuver():
    document = manifest_for(_row("forcing-20ms")).to_doc()

    assert set(document["participants"]) == {"maneuver", "plant"}
    assert document["participants"]["maneuver"]["step_period_ns"] == MANEUVER_PERIOD_NS
    assert document["participants"]["plant"]["step_period_ns"] == 20_000_000
    assert document["participants"]["plant"]["subscribes"][0]["channel"] == "maneuver"


def test_sampling_manifest_keeps_plant_and_channel_latency_fixed():
    document = manifest_for(_row("sampling-5ms")).to_doc()

    assert document["participants"]["plant"]["step_period_ns"] == 10_000_000
    assert document["participants"]["controller"]["step_period_ns"] == 5_000_000
    assert document["channels"]["sensing"]["latency_ns"] == 10_000_000
    assert document["channels"]["command"]["latency_ns"] == 10_000_000
    assert document["participants"]["controller"]["subscribes"][0]["capacity"] == route_capacity(
        10_000_000, 5_000_000, 10_000_000
    )


def test_evidence_writer_emits_reviewable_plots_and_data(tmp_path):
    results = [{
        "row": {"name": "plant-20ms", "family": "plant-period-sensitivity"},
        "comparison": {
            "max_abs_error": {"lead_position_m": 0.1, "gap_m": 0.2},
            "minimum_gap_m": 43.0,
            "reference_minimum_gap_m": 43.1,
        },
    }]

    paths = write_plots(tmp_path, results)

    assert paths == [
        "sensitivity-error.svg", "sensitivity-kpi.svg", "sensitivity-plot-data.csv",
    ]
    assert all((tmp_path / path).is_file() for path in paths)


def test_readable_table_shows_reference_and_verdict_comparisons():
    result = {
        "row": {
            "name": "timing-defect",
            "family": "negative-control",
            "periods_ns": {
                "plant": 10_000_000,
                "controller": 10_000_000,
                "maneuver": 1_000_000,
                "kpi": 20_000_000,
            },
            "latencies_ns": {
                "sensing": 250_000_000,
                "command": 10_000_000,
                "maneuver": 0,
                "state": 0,
            },
            "reference": "fine-timing-defect",
            "sensitivity_reference": "latency-10ms",
        },
        "comparison": {
            "max_abs_error": {
                "lead_position_m": 0.01,
                "ego_speed_mps": 0.02,
                "accel_mps2": 0.0222354,
            },
            "minimum_gap_m": 42.0,
        },
        "sensitivity_comparison": {
            "max_abs_error": {
                "gap_m": 0.3,
                "relative_speed_mps": 0.4,
                "accel_mps2": 0.641677,
            },
            "minimum_gap_delta_m": 0.7,
        },
        "exceeded_envelope": ["accel_mps2"],
    }

    table = markdown_table([result])

    assert "0.01 / 0.02 / 0.0222354" in table
    assert "latency-10ms | 0.3 / 0.4 / 0.641677 | 0.7" in table
    assert "EXCEEDED: accel_mps2" in table
