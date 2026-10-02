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

The Native form (#232) runs the same events through `native_adapter.c`:

- **Event time.** The Native ABI's `take` returns a payload without its
  Message time, so each frame carries its recorded time in `event_ns`.
  `sil-window` rebases that field with the log time, so it is the Burst's
  Virtual instant, as `publish_ns` is for the Process adapter.
- **Transmit.** The recorded camera frames openpilot replaces become
  transmit candidates on `can.tx` (see `prepare_transmit.py`). The
  observation adds their accepted and rejected counts.
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

# The Native form (#232).
TRANSMIT_CHANNEL = "can.tx"
TIMED_FRAME_SCHEMA = "can.TimedFrame"
NATIVE_STATE_SCHEMA = "libsafety.NativeState"
NATIVE_REFERENCE_SCHEMA = "libsafety.NativeReferenceState"
# STEERING_LTA, STEERING_LKA, ACC_CONTROL and LKAS_HUD: the Toyota stock
# camera sends them on bus 2, and openpilot sends them in its place on bus 0.
TRANSMIT_ADDRESSES = (0x191, 0x2E4, 0x343, 0x412)
CAMERA_BUS = 2
TRANSMIT_BUS = 0
# The candidates are not in every event; 100 ms still rejects a lost burst
# of camera frames, which come at 20 to 42 Hz per address.
TRANSMIT_MAX_GAP_NS = 100_000_000

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
SCHEMAS[TIMED_FRAME_SCHEMA] = {"fields": [
    {"name": "event_ns", "type": "u64"}, *SCHEMAS[FRAME_SCHEMA]["fields"]]}
# The transmit verdicts follow the receive verdicts.
SCHEMAS[NATIVE_STATE_SCHEMA] = {"fields": [
    added for field in SCHEMAS[STATE_SCHEMA]["fields"]
    for added in ([field, {"name": "tx_accepted", "type": "u32"},
                   {"name": "tx_rejected", "type": "u32"}]
                  if field["name"] == "rejected" else [field])]}


def _without_config_valid(schema: str) -> dict:
    return {"fields": [field for field in SCHEMAS[schema]["fields"]
                       if field["name"] != "config_valid"]}


SCHEMAS[REFERENCE_SCHEMA] = _without_config_valid(STATE_SCHEMA)
SCHEMAS[NATIVE_REFERENCE_SCHEMA] = _without_config_valid(NATIVE_STATE_SCHEMA)
STATE_FIELDS = [field["name"] for field in SCHEMAS[STATE_SCHEMA]["fields"]]
NATIVE_STATE_FIELDS = [field["name"]
                       for field in SCHEMAS[NATIVE_STATE_SCHEMA]["fields"]]


def _mapping(timestamp_column: str, channel: str, schema: str,
             columns: dict | None = None) -> dict:
    columns = columns or {}
    return {
        "sil_csv_mapping": 1,
        "timestamp": {"column": timestamp_column, "unit": "ns"},
        "schemas": {schema: SCHEMAS[schema]},
        "channels": [{
            "channel": channel, "schema": schema,
            "fields": {field["name"]: {"column": columns.get(field["name"],
                                                             field["name"])}
                       for field in SCHEMAS[schema]["fields"]},
        }],
    }


FRAME_MAPPING = _mapping("log_mono_ns", FRAME_CHANNEL, FRAME_SCHEMA)
REFERENCE_MAPPING = _mapping("slot_ns", STATE_CHANNEL, REFERENCE_SCHEMA)
# The recorded time feeds the Message time and the frame's own event_ns.
TIMED_FRAME_MAPPING = _mapping("log_mono_ns", FRAME_CHANNEL, TIMED_FRAME_SCHEMA,
                               {"event_ns": "log_mono_ns"})
TRANSMIT_MAPPING = _mapping("log_mono_ns", TRANSMIT_CHANNEL, TIMED_FRAME_SCHEMA,
                            {"event_ns": "log_mono_ns"})
NATIVE_REFERENCE_MAPPING = _mapping("slot_ns", STATE_CHANNEL,
                                    NATIVE_REFERENCE_SCHEMA)


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


def timed_window_document(first_log_mono_ns: int, last_log_mono_ns: int,
                          channel: str, max_gap_ns: int,
                          channel_start_ns: int | None = None) -> dict:
    """The same window for a timed frame Channel: its event_ns is rebased
    with the log time. A Channel that starts later than the first event, as
    the transmit candidates may, starts its replay there; the origin, and so
    Virtual time, stays the first event's."""
    start = first_log_mono_ns if channel_start_ns is None else channel_start_ns
    return {**window_document(first_log_mono_ns, last_log_mono_ns),
            "replay_start_ns": start, "evaluation_start_ns": start,
            "channels": [channel], "max_gap_ns": max_gap_ns,
            "source_time_fields": {channel: ["event_ns"]}}


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


def native_reference_rows(trace: list[dict],
                          first_log_mono_ns: int) -> list[dict]:
    """The Native reference rows: the receive rows and the transmit verdicts."""
    return [{**row, "tx_accepted": state["tx_accepted"],
             "tx_rejected": state["tx_rejected"]}
            for row, state in zip(reference_rows(trace, first_log_mono_ns),
                                  trace)]


def _rule(field: str):
    if field == "config_valid":
        # The reference does not record it; acceptance checks it separately.
        return "ignore"
    return {"atol": 0, "rtol": 0} if field in FLOAT_STATE else "exact"


def comparison_contract(slots: list[int],
                        fields: list[str] = STATE_FIELDS) -> dict:
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
            "fields": {name: _rule(name) for name in fields},
        }},
    }
