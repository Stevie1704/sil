"""The observation convention and the comparison contract of the replay."""
import pytest
from workload import (
    STATE_CHANNEL,
    STEP_PERIOD_NS,
    comparison_contract,
    observation_slot,
    reference_rows,
)

ORIGIN_NS = 18_054_876_797_669


@pytest.mark.parametrize("event_ns, slot_ns", [
    (0, 0), (1, 1_000_000), (9_763_346, 10_000_000), (2_000_000, 2_000_000)])
def test_a_burst_is_observed_in_the_first_step_at_or_after_its_instant(
        event_ns, slot_ns):
    assert STEP_PERIOD_NS == 1_000_000
    assert observation_slot(event_ns) == slot_ns


def test_a_reference_row_names_its_slot_its_event_and_every_state_field():
    trace = [{"t_ns": ORIGIN_NS + 9_763_346, "accepted": 39, "rejected": 0,
              "controls_allowed": True, "gas_pressed_prev": False,
              "brake_pressed_prev": False, "cruise_engaged_prev": True,
              "vehicle_moving": True, "acc_main_on": False,
              "vehicle_speed_min": 0.0, "vehicle_speed_max": 25.740999221801758}]
    assert reference_rows(trace, ORIGIN_NS) == [{
        "slot_ns": 10_000_000, "event_ns": 9_763_346, "accepted": 39,
        "rejected": 0, "controls_allowed": 1, "gas_pressed_prev": 0,
        "brake_pressed_prev": 0, "cruise_engaged_prev": 1,
        "vehicle_moving": 1, "acc_main_on": 0,
        "vehicle_speed_min": "0.0", "vehicle_speed_max": "25.740999221801758"}]


def test_the_contract_observes_every_slot_exactly_through_the_last_one():
    contract = comparison_contract([0, 10_000_000, 60_000_000])
    assert contract["evaluation"] == {"from_ns": 0, "to_ns": 60_999_999}
    channel = contract["channels"][STATE_CHANNEL]
    assert channel["observations"] == {"times_ns": [0, 10_000_000, 60_000_000]}
    assert channel["actual_offset_ns"] == channel["reference_offset_ns"] == 0
    assert channel["fields"]["event_ns"] == "exact"
    assert channel["fields"]["controls_allowed"] == "exact"
    assert channel["fields"]["vehicle_speed_max"] == {"atol": 0, "rtol": 0}
    assert len(channel["fields"]) == 11
