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
    assert channel["fields"]["config_valid"] == "ignore"
    assert len(channel["fields"]) == 12


# The Native form (#232) ---------------------------------------------------------

def test_a_timed_frame_takes_its_event_time_from_the_recorded_time():
    from workload import TIMED_FRAME_MAPPING, TIMED_FRAME_SCHEMA, SCHEMAS
    fields = TIMED_FRAME_MAPPING["channels"][0]["fields"]
    assert TIMED_FRAME_MAPPING["timestamp"] == {"column": "log_mono_ns",
                                                "unit": "ns"}
    assert fields["event_ns"] == {"column": "log_mono_ns"}
    assert fields["address"] == {"column": "address"}
    assert [f["name"] for f in SCHEMAS[TIMED_FRAME_SCHEMA]["fields"]][:2] == [
        "event_ns", "address"]


def test_the_timed_window_rebases_event_ns_and_keeps_the_first_event_origin():
    from workload import TRANSMIT_CHANNEL, timed_window_document
    window = timed_window_document(ORIGIN_NS, ORIGIN_NS + 60, TRANSMIT_CHANNEL,
                                   100, channel_start_ns=ORIGIN_NS + 7)
    assert window["source_origin_ns"] == ORIGIN_NS
    assert window["replay_start_ns"] == window["evaluation_start_ns"] == ORIGIN_NS + 7
    assert window["end_ns"] == ORIGIN_NS + 61
    assert window["channels"] == [TRANSMIT_CHANNEL]
    assert window["source_time_fields"] == {TRANSMIT_CHANNEL: ["event_ns"]}
    assert window["max_gap_ns"] == 100


def test_a_native_reference_row_adds_the_transmit_verdicts():
    from workload import native_reference_rows
    trace = [{"t_ns": ORIGIN_NS, "accepted": 39, "rejected": 0,
              "tx_accepted": 2, "tx_rejected": 1,
              "controls_allowed": True, "gas_pressed_prev": False,
              "brake_pressed_prev": False, "cruise_engaged_prev": True,
              "vehicle_moving": True, "acc_main_on": False,
              "vehicle_speed_min": 0.0, "vehicle_speed_max": 1.5}]
    [row] = native_reference_rows(trace, ORIGIN_NS)
    assert (row["slot_ns"], row["event_ns"]) == (0, 0)
    assert (row["tx_accepted"], row["tx_rejected"]) == (2, 1)


def test_the_native_contract_compares_the_transmit_verdicts_exactly():
    from workload import NATIVE_STATE_FIELDS
    channel = comparison_contract([0], NATIVE_STATE_FIELDS)["channels"][STATE_CHANNEL]
    assert channel["fields"]["tx_accepted"] == "exact"
    assert channel["fields"]["tx_rejected"] == "exact"
    assert channel["fields"]["config_valid"] == "ignore"
    assert len(channel["fields"]) == 14


def test_the_candidates_are_the_camera_frames_openpilot_replaces_on_bus_0():
    from prepare_transmit import transmit_candidates
    frames = [(0x2E4, 2, b"\x80\x00\x00\x00\x6b"),  # STEERING_LKA from the camera
              (0x2E4, 0, b"\x00"),                   # not from the camera
              (0x260, 2, b"\x01"),                   # not a replaced message
              (0x191, 2, b"\x02"), (0x343, 2, b"\x03"), (0x412, 2, b"\x04"),
              (0x2E4, 130, b"\x05")]                 # a transmit echo
    assert transmit_candidates(frames) == [
        (0x2E4, 0, b"\x80\x00\x00\x00\x6b"), (0x191, 0, b"\x02"),
        (0x343, 0, b"\x03"), (0x412, 0, b"\x04")]


def test_the_native_manifest_declares_one_adapter_with_the_recorded_contract(tmp_path):
    import manifest
    frames, transmit = tmp_path / "frames.mcap", tmp_path / "transmit.mcap"
    frames.write_bytes(b"frames")
    transmit.write_bytes(b"transmit")
    segment = manifest.Segment(first_log_mono_ns=ORIGIN_NS, last_event_ns=59_990_294_747,
                               mode=2, param=73, alternative_experience=0,
                               largest_burst=47, largest_transmit_burst=3)
    doc = manifest.native_manifest(frames, transmit, tmp_path / "adapter.so",
                                   tmp_path / "libsafety.so", segment).to_doc()
    entry = doc["participants"]["libsafety"]
    assert entry["type"] == "native"
    assert entry["config"]["param"] == 73
    assert entry["config"]["timer_origin_ns"] == ORIGIN_NS
    assert entry["config"]["period_ns"] == STEP_PERIOD_NS
    assert [r["capacity"] for r in entry["subscribes"]] == [47, 3]
    assert doc["duration_ns"] == 59_992_000_000
    assert {name for name, p in doc["participants"].items()
            if p["type"] == "native"} == {"libsafety"}


def test_the_second_instance_control_adds_a_participant_of_the_same_adapter(tmp_path):
    import manifest
    for name in ("frames.mcap", "transmit.mcap"):
        (tmp_path / name).write_bytes(b"x")
    segment = manifest.Segment(ORIGIN_NS, 1_000, 2, 73, 0, 47, 3)
    doc = manifest.native_manifest(tmp_path / "frames.mcap", tmp_path / "transmit.mcap",
                                   tmp_path / "adapter.so", tmp_path / "libsafety.so",
                                   segment, **manifest.NATIVE_CONTROLS["second-instance"]).to_doc()
    second = doc["participants"]["libsafety-2"]
    assert second["library"] == doc["participants"]["libsafety"]["library"]
    assert second["publishes"] == ["libsafety.state.2"]
