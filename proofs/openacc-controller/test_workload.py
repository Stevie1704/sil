"""The conversion, authoring, observation and control policy of the replay."""
import json
from pathlib import Path

import pytest
from workload import (
    COMMAND_CHANNEL,
    CONTROLS,
    DURATION_NS,
    PERIOD_NS,
    REFERENCE_CHANNEL,
    SENSING_CHANNEL,
    Variant,
    authoring_document,
    comparison_contract,
    first_divergence,
    input_mapping,
    predicted_divergence,
    recorded_inputs,
    reference_rows,
)

HANDOFF = (Path(__file__).resolve().parents[1]
           / "public-workloads" / "evidence" / "handoff.json")
HEADER = ("Time,Speed1,Speed2,IVS1,t_ns,gap_m,relative_speed_mps,"
          "ego_speed_mps,lead_accel_mps2")


def window_csv(*rows: str) -> str:
    return "\n".join((HEADER, *rows)) + "\n"


def sample(t_ns, gap, relative, ego):
    return {"t_ns": t_ns, "gap_m": gap, "relative_speed_mps": relative,
            "ego_speed_mps": ego}


SAMPLES = [sample(0, 30.0, -1.5, 27.0), sample(PERIOD_NS, 31.0, -0.5, 26.0),
           sample(2 * PERIOD_NS, 32.0, 0.5, 25.0)]


def test_the_run_contract_is_the_committed_handoffs():
    pins = json.loads(HANDOFF.read_text())["single_fmu_194"]
    assert (PERIOD_NS, DURATION_NS) == (pins["period_ns"], pins["duration_ns"])
    assert DURATION_NS // PERIOD_NS == pins["recording"]["samples"]
    field = comparison_contract()["channels"][COMMAND_CHANNEL]["fields"]
    assert field["accel_mps2"] == {"atol": pins["comparison"]["absolute"],
                                   "rtol": pins["comparison"]["relative"]}


def test_the_recorded_window_converts_by_the_declared_policy():
    text = window_csv("300.0,27.5,27.0,30.25,0,30.25,0.5,27.0,-1.0",
                      "300.1,27.4,27.1,30.3,100000000,30.3,"
                      f"{27.4 - 27.1!r},27.1,")
    assert recorded_inputs(text, window_start_s=300.0) == [
        sample(0, 30.25, 0.5, 27.0),
        sample(PERIOD_NS, 30.3, 27.4 - 27.1, 27.1)]


@pytest.mark.parametrize("row, reason", [
    ("300.0,27.5,27.0,30.25,0,34.05,0.5,27.0,", "gap_m"),
    ("300.0,27.5,27.0,30.25,0,30.25,-0.5,27.0,", "relative_speed_mps"),
    ("300.0,27.5,27.0,30.25,0,30.25,0.5,27.5,", "ego_speed_mps"),
    ("300.0,27.5,27.0,30.25,1,30.25,0.5,27.0,", "t_ns"),
])
def test_a_derived_column_that_departs_from_the_policy_is_refused(row, reason):
    with pytest.raises(ValueError, match=reason):
        recorded_inputs(window_csv(row), window_start_s=300.0)


def test_a_sample_off_the_grid_is_refused():
    text = window_csv("300.0,27.5,27.0,30.25,0,30.25,0.5,27.0,",
                      "300.2,27.5,27.0,30.25,200000000,30.25,0.5,27.0,")
    with pytest.raises(ValueError, match="grid"):
        recorded_inputs(text, window_start_s=300.0)


def test_the_input_mapping_reads_the_derived_columns_on_the_ns_timestamp():
    mapping = input_mapping(Variant())
    assert mapping["timestamp"] == {"column": "t_ns", "unit": "ns"}
    (channel,) = mapping["channels"]
    assert channel["channel"] == SENSING_CHANNEL
    assert channel["fields"] == {name: {"column": name} for name in
                                 ("gap_m", "relative_speed_mps", "ego_speed_mps")}


def test_the_changed_input_inverts_the_relative_speed_at_the_edge():
    fields = input_mapping(CONTROLS["changed-input"])["channels"][0]["fields"]
    assert fields["relative_speed_mps"] == {"column": "relative_speed_mps",
                                            "scale": -1}
    assert fields["gap_m"] == {"column": "gap_m"}


def test_the_nominal_document_states_sample_zero_as_the_start_values():
    document = authoring_document(Variant(), SAMPLES[0])
    assert document["step_period_ns"] == PERIOD_NS
    assert document["duration_ns"] == DURATION_NS
    assert document["channels"][SENSING_CHANNEL]["latency_ns"] == 0
    assert document["channels"][SENSING_CHANNEL]["route"] == {
        "capacity": 1, "overflow": "fail"}
    assert document["start"] == [
        {"variable": "gap_m", "value": "30.0", "unit": "m"},
        {"variable": "relative_speed_mps", "value": "-1.5", "unit": "m/s"},
        {"variable": "ego_speed_mps", "value": "27.0", "unit": "m/s"}]
    assert {(b["field"], b["variable"]) for b in document["bind"]} == {
        ("gap_m", "gap_m"), ("relative_speed_mps", "relative_speed_mps"),
        ("ego_speed_mps", "ego_speed_mps"), ("accel_mps2", "accel_mps2")}
    assert document["hold"] == []


def test_each_control_changes_one_thing_in_the_document():
    nominal = authoring_document(Variant(), SAMPLES[0])
    shift = authoring_document(CONTROLS["one-period-shift"], SAMPLES[0])
    assert shift["channels"][SENSING_CHANNEL]["latency_ns"] == PERIOD_NS
    assert {**shift, "channels": nominal["channels"]} == nominal
    swapped = authoring_document(CONTROLS["wrong-binding"], SAMPLES[0])
    assert {(b["field"], b["variable"]) for b in swapped["bind"]} >= {
        ("relative_speed_mps", "ego_speed_mps"),
        ("ego_speed_mps", "relative_speed_mps")}
    assert {**swapped, "bind": nominal["bind"]} == nominal
    declared = authoring_document(CONTROLS["declared-starts"], SAMPLES[0])
    assert {**declared, "start": nominal["start"]} == nominal
    assert declared["start"] == []
    assert authoring_document(CONTROLS["changed-input"], SAMPLES[0]) == nominal


def test_the_fmu_sees_each_sample_in_its_own_step_unless_shifted():
    assert Variant().seen(SAMPLES) == SAMPLES
    shifted = CONTROLS["one-period-shift"].seen(SAMPLES)
    # Step 0 holds the start values, which are sample 0.
    assert [s["gap_m"] for s in shifted] == [30.0, 30.0, 31.0]
    inverted = CONTROLS["changed-input"].seen(SAMPLES)
    assert [s["relative_speed_mps"] for s in inverted] == [1.5, 0.5, -0.5]
    swapped = CONTROLS["wrong-binding"].seen(SAMPLES)
    assert swapped[0]["relative_speed_mps"] == 27.0
    assert swapped[0]["ego_speed_mps"] == -1.5


def test_the_reference_rows_keep_each_command_at_the_time_it_describes():
    trace = [{"t_ns": PERIOD_NS, "accel_mps2": -3.0},
             {"t_ns": 2 * PERIOD_NS, "accel_mps2": 0.1 + 0.2}]
    assert reference_rows(trace) == [
        {"t_ns": PERIOD_NS, "accel_mps2": "-3.0"},
        {"t_ns": 2 * PERIOD_NS, "accel_mps2": "0.30000000000000004"}]


def test_the_contract_observes_every_command_through_the_final_one():
    contract = comparison_contract()
    assert contract["evaluation"] == {"from_ns": PERIOD_NS, "to_ns": DURATION_NS}
    channel = contract["channels"][COMMAND_CHANNEL]
    assert channel["reference_channel"] == REFERENCE_CHANNEL
    # Published in Slot t_k, the command describes t_k + one period.
    assert channel["actual_offset_ns"] == PERIOD_NS
    assert channel["reference_offset_ns"] == 0
    assert channel["observations"] == {
        "start_ns": PERIOD_NS, "stop_ns": DURATION_NS, "step_ns": PERIOD_NS}


def test_a_divergence_is_judged_by_sil_compares_rule():
    reference = [{"t_ns": PERIOD_NS, "accel_mps2": 1.0},
                 {"t_ns": 2 * PERIOD_NS, "accel_mps2": 1.0}]
    # 1.0000000827e-10 away: outside atol alone, inside atol + rtol * 1.0.
    inside = 1.0 + 1e-10
    assert abs(inside - 1.0) > 1e-10
    assert first_divergence(reference, [1.0, inside]) is None
    assert first_divergence(reference, [1.0, 1.0 + 2e-10]) == {
        "index": 1, "observation_ns": 2 * PERIOD_NS,
        "expected": 1.0, "actual": 1.0 + 2e-10}


def test_the_expected_divergence_is_the_law_over_what_the_fmu_sees():
    def law(gap_m, relative_speed_mps, ego_speed_mps):
        return gap_m
    reference = [{"t_ns": (k + 1) * PERIOD_NS, "accel_mps2": s["gap_m"]}
                 for k, s in enumerate(SAMPLES)]
    assert predicted_divergence(Variant(), SAMPLES, reference, law) is None
    assert predicted_divergence(CONTROLS["one-period-shift"], SAMPLES,
                                reference, law) == {
        "index": 1, "observation_ns": 2 * PERIOD_NS,
        "expected": 31.0, "actual": 30.0}
