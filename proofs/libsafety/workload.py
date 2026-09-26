"""The libsafety replay's Channels, conversion, window and comparison (#193).

Everything a Run and its judgement decide is stated here once, so the
Manifest, the conversion mappings and the comparison contract cannot
disagree:

- **Frames.** One CSV row per received frame (`src` < 128), in recorded
  order. `sil-csv` converts it into the `can.rx` Channel, keeping the
  recorded `logMonoTime` as the Message time. Frames of one event share a
  time, so they are one Burst in Publish order.
- **Window.** `sil-window` rebases the Recording so that the first event is
  at Virtual time 0 and selects every event. The warm-up is empty: the
  library starts from `set_safety_hooks` at the recording's first event, as
  the reference does, and the library's own warm-up (no `safety_tick` in the
  first and last second) runs inside the evaluation and is compared.
- **Observation.** A Burst at Virtual time `e` is visible, with Latency 0,
  in the first Step at or after `e`, and its observation is published in
  that Slot. The reference row of each event is published in the same Slot
  and names `e` in `event_ns`, which is compared exactly.
- **Comparison.** Every field the reference records is compared exactly at
  every event, from the first observation through the last. The reference
  does not record `config_valid`; acceptance requires it to be 1 at every
  nominal event that ran `safety_tick`, as upstream does.
"""

from __future__ import annotations

STEP_PERIOD_NS = 1_000_000
FRAME_CHANNEL = "can.rx"
STATE_CHANNEL = "libsafety.state"
FRAME_SCHEMA = "can.Frame"
STATE_SCHEMA = "libsafety.State"
# The reference records every observed field except config_valid.
REFERENCE_SCHEMA = "libsafety.ReferenceState"
# Received frames only: sources 128 and above are transmit echoes.
TRANSMIT_ECHO_SOURCE = 128
# A recorded event interval is at least 8.75 ms; 12 ms rejects a lost event.
MAX_GAP_NS = 12_000_000

FRAME_COLUMNS = ("log_mono_ns", "address", "src", "length",
                 *(f"d{i}" for i in range(8)))
BOOL_STATE = ("controls_allowed", "gas_pressed_prev", "brake_pressed_prev",
              "cruise_engaged_prev", "vehicle_moving", "acc_main_on")
FLOAT_STATE = ("vehicle_speed_min", "vehicle_speed_max")

SCHEMAS = {
    FRAME_SCHEMA: {"fields": [
        {"name": "address", "type": "u32"},
        {"name": "src", "type": "u8"},
        {"name": "length", "type": "u8"},
        *({"name": f"d{i}", "type": "u8"} for i in range(8)),
    ]},
    STATE_SCHEMA: {"fields": [
        {"name": "event_ns", "type": "u64"},
        {"name": "accepted", "type": "u32"},
        {"name": "rejected", "type": "u32"},
        {"name": "config_valid", "type": "u8"},
        *({"name": name, "type": "u8"} for name in BOOL_STATE),
        # The library returns C floats; f32 keeps them bit for bit.
        *({"name": name, "type": "f32"} for name in FLOAT_STATE),
    ]},
}
SCHEMAS[REFERENCE_SCHEMA] = {"fields": [
    field for field in SCHEMAS[STATE_SCHEMA]["fields"]
    if field["name"] != "config_valid"]}
STATE_FIELDS = [field["name"] for field in SCHEMAS[STATE_SCHEMA]["fields"]]


def _mapping(timestamp_column: str, channel: str, schema: str) -> dict:
    return {
        "sil_csv_mapping": 1,
        "timestamp": {"column": timestamp_column, "unit": "ns"},
        "schemas": {schema: SCHEMAS[schema]},
        "channels": [{
            "channel": channel, "schema": schema,
            "fields": {field["name"]: {"column": field["name"]}
                       for field in SCHEMAS[schema]["fields"]},
        }],
    }


FRAME_MAPPING = _mapping("log_mono_ns", FRAME_CHANNEL, FRAME_SCHEMA)
REFERENCE_MAPPING = _mapping("slot_ns", STATE_CHANNEL, REFERENCE_SCHEMA)


def window_document(first_log_mono_ns: int, last_log_mono_ns: int) -> dict:
    """The whole segment, rebased to its first event, with no warm-up."""
    return {
        "sil_replay_window": 1,
        "source_origin_ns": first_log_mono_ns,
        "replay_start_ns": first_log_mono_ns,
        "evaluation_start_ns": first_log_mono_ns,
        "end_ns": last_log_mono_ns + 1,
        "channels": [FRAME_CHANNEL],
        "max_gap_ns": MAX_GAP_NS,
    }


def observation_slot(event_ns: int) -> int:
    """The first Step at or after the Burst's instant."""
    return -(-event_ns // STEP_PERIOD_NS) * STEP_PERIOD_NS


def duration_ns(last_event_ns: int) -> int:
    """A Duration that runs the Step observing the last event."""
    return observation_slot(last_event_ns) + STEP_PERIOD_NS


def reference_rows(trace: list[dict], first_log_mono_ns: int) -> list[dict]:
    """The independent reference as the CSV rows `REFERENCE_MAPPING` reads."""
    rows = []
    for state in trace:
        event_ns = state["t_ns"] - first_log_mono_ns
        rows.append({
            "slot_ns": observation_slot(event_ns), "event_ns": event_ns,
            "accepted": state["accepted"], "rejected": state["rejected"],
            **{name: int(state[name]) for name in BOOL_STATE},
            # repr is the shortest exact decimal of the binary64 value.
            **{name: repr(float(state[name])) for name in FLOAT_STATE},
        })
    return rows


def _rule(field: str):
    if field == "config_valid":
        # The reference does not record it; acceptance checks it separately.
        return "ignore"
    return {"atol": 0, "rtol": 0} if field in FLOAT_STATE else "exact"


def comparison_contract(slots: list[int]) -> dict:
    """Every recorded field exact at every observation Slot, the last one
    included."""
    return {
        "sil_comparison": 1,
        "evaluation": {"from_ns": slots[0],
                       "to_ns": slots[-1] + STEP_PERIOD_NS - 1},
        "channels": {STATE_CHANNEL: {
            "actual_offset_ns": 0,
            "reference_offset_ns": 0,
            "observations": {"times_ns": list(slots)},
            "fields": {name: _rule(name) for name in STATE_FIELDS},
        }},
    }
