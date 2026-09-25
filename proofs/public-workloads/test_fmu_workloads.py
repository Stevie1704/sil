"""Comparison and input policies of the FMU workloads, without an FMU."""
import pytest
from fmu_workloads import held, numeric_divergence, shifted


def test_a_one_period_shift_holds_the_first_sample():
    assert shifted([1, 2, 3], 1) == [1, 1, 2]
    assert shifted([1, 2, 3], 0) == [1, 2, 3]


def test_a_recorded_sample_is_held_for_its_whole_interval():
    samples = [10.0, 20.0]
    assert [held(samples, t, 100) for t in (0, 99, 100, 199)] == [10.0, 10.0, 20.0, 20.0]
    with pytest.raises(IndexError):
        held(samples, 200, 100)


def rows(*values):
    return [{"t_ns": i * 100, "accel_mps2": v} for i, v in enumerate(values)]


def test_agreement_inside_tolerance_is_no_divergence():
    assert numeric_divergence(rows(1.0, 2.0), rows(1.0, 2.0 + 1e-13), 1e-10, 1e-12) is None


def test_the_first_divergence_names_time_field_and_both_values():
    divergence = numeric_divergence(rows(1.0, 2.0, 3.0), rows(1.0, 2.5, 3.5), 1e-10, 1e-12)
    assert divergence == {"index": 1, "t_ns": 100, "field": "accel_mps2",
                          "expected": 2.0, "actual": 2.5}


def test_a_missing_final_sample_diverges_on_coverage():
    assert numeric_divergence(rows(1.0, 2.0), rows(1.0), 1e-10, 1e-12)["field"] == "coverage"


def test_a_non_finite_value_diverges():
    assert numeric_divergence(rows(1.0), rows(float("nan")), 1e-10, 1e-12)["index"] == 0
