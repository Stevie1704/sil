"""Independent, exact-time comparisons for the sensitivity report."""
from __future__ import annotations

import math
import struct

from sil.recording import read_records

from sensitivity_contract import (
    ACCEPTANCE_ENVELOPE,
    CHANNEL_FIELDS,
    CONSTANT_ACCELERATION_MPS2,
)


def analytic_state(time_ns: int, acceleration_mps2: float) -> dict[str, float]:
    """The constant-acceleration oracle, written independently of the FMU code."""
    t = time_ns / 1_000_000_000
    ego_position = 25.0 * t + 0.5 * acceleration_mps2 * t * t
    ego_speed = 25.0 + acceleration_mps2 * t
    lead_position = 60.0 + 25.0 * t
    return {
        "ego_position_m": ego_position,
        "ego_speed_mps": ego_speed,
        "lead_position_m": lead_position,
        "lead_speed_mps": 25.0,
        "gap_m": lead_position - ego_position,
        "relative_speed_mps": 25.0 - ego_speed,
    }


def compare_observations(
    measured: dict[str, dict[int, list[float]]],
    reference: dict[str, dict[int, list[float]]],
    fields: dict[str, list[str]],
) -> dict:
    """Compare only native endpoint samples; never synthesize an intermediate one."""
    maximum: dict[str, float] = {field: 0.0 for names in fields.values() for field in names}
    samples = {}
    for channel, channel_fields in fields.items():
        actual = measured.get(channel, {})
        expected = reference.get(channel, {})
        channel_samples = 0
        for time_ns, values in sorted(actual.items()):
            if time_ns not in expected:
                raise RuntimeError(
                    f"missing exact observation time {time_ns} ns for {channel}; "
                    "interpolation is not permitted"
                )
            wanted = expected[time_ns]
            if len(values) != len(channel_fields) or len(wanted) != len(channel_fields):
                raise RuntimeError(f"width mismatch for {channel} at {time_ns} ns")
            for field, observed, target in zip(channel_fields, values, wanted):
                if not math.isfinite(observed) or not math.isfinite(target):
                    raise RuntimeError(f"nonfinite {field} at {time_ns} ns")
                error = abs(observed - target)
                maximum[field] = max(maximum[field], error)
            channel_samples += 1
        if not actual:
            raise RuntimeError(f"no observations for {channel}")
        samples[channel] = channel_samples
    return {"max_abs_error": maximum, "sample_count": samples}


def recording_observations(path, row: dict) -> dict[str, dict[int, list[float]]]:
    """Decode native endpoint times from a Recording without resampling it."""
    scenario = row["scenario"]
    periods = row["periods_ns"]
    if scenario == "constant-acceleration":
        channels = {"state": (periods["plant"], [
            "ego_position_m", "ego_speed_mps", "lead_position_m",
            "lead_speed_mps", "gap_m", "relative_speed_mps",
        ])}
    else:
        channels = {
            "sensing": (periods["plant"], ["gap_m", "relative_speed_mps", "ego_speed_mps"]),
            "state": (periods["plant"], [
                "ego_position_m", "ego_speed_mps", "lead_position_m",
                "lead_speed_mps", "gap_m", "relative_speed_mps",
            ]),
            "command": (periods["controller"], ["accel_mps2"]),
        }
    observations = {channel: {} for channel in channels}
    for channel, publish_ns, payload in read_records(path):
        if channel not in channels:
            raise RuntimeError(f"unknown Channel {channel} in sensitivity Recording")
        period_ns, fields = channels[channel]
        if len(payload) != 8 * len(fields):
            raise RuntimeError(f"width mismatch for {channel} at {publish_ns} ns")
        endpoint_ns = publish_ns + period_ns
        if endpoint_ns > row["duration_ns"] or endpoint_ns % row["observation_grid_ns"]:
            raise RuntimeError(
                f"{channel} at {publish_ns} ns does not land on the exact "
                "observation grid"
            )
        if endpoint_ns in observations[channel]:
            raise RuntimeError(f"duplicate exact observation time {endpoint_ns} ns for {channel}")
        observations[channel][endpoint_ns] = list(
            struct.unpack("<" + "d" * len(fields), payload)
        )
    expected = {
        channel: set(range(period, row["duration_ns"] + 1, period))
        for channel, (period, _fields) in channels.items()
    }
    for channel, wanted in expected.items():
        actual = set(observations[channel])
        if actual != wanted:
            raise RuntimeError(
                f"missing/extra exact observations for {channel}: "
                f"expected {sorted(wanted)[:3]}... got {sorted(actual)[:3]}..."
            )
    return observations


def reference_observations(document: dict, row: dict) -> dict[str, dict[int, list[float]]]:
    """Decode the independent driver's endpoint trace on exact integer times."""
    observations = {channel: {} for channel in CHANNEL_FIELDS}
    for channel, messages in document.get("messages", {}).items():
        if channel not in observations:
            raise RuntimeError(f"unknown reference Channel {channel}")
        for message in messages:
            endpoint_ns = message["endpoint_ns"]
            if endpoint_ns % row["observation_grid_ns"]:
                raise RuntimeError(f"reference endpoint is off-grid: {endpoint_ns} ns")
            if endpoint_ns in observations[channel]:
                raise RuntimeError(f"duplicate reference endpoint {endpoint_ns} ns for {channel}")
            observations[channel][endpoint_ns] = list(message["values"])
    periods = row["periods_ns"]
    expected_periods = {
        "sensing": periods["plant"],
        "state": periods["plant"],
        "command": periods["controller"],
    }
    for channel, period_ns in expected_periods.items():
        if period_ns is None:
            continue
        expected = set(range(period_ns, row["duration_ns"] + 1, period_ns))
        actual = set(observations[channel])
        if actual != expected:
            raise RuntimeError(f"reference missing/extra endpoints for {channel}")
    return {channel: values for channel, values in observations.items() if values}


def analytic_observations(row: dict) -> dict[str, dict[int, list[float]]]:
    period_ns = row["periods_ns"]["plant"]
    values = {}
    for endpoint_ns in range(period_ns, row["duration_ns"] + 1, period_ns):
        state = analytic_state(endpoint_ns, CONSTANT_ACCELERATION_MPS2)
        values[endpoint_ns] = [state[field] for field in CHANNEL_FIELDS["state"]]
    return {"state": values}


def kpi_metrics(observations: dict[str, dict[int, list[float]]]) -> dict:
    sensing = observations.get("sensing", {})
    state = observations.get("state", {})
    command = observations.get("command", {})
    gaps = [(values[0], time_ns) for time_ns, values in sensing.items()]
    if not gaps:
        gaps = [(values[4], time_ns) for time_ns, values in state.items()]
    minimum_gap, minimum_gap_at_ns = min(gaps)
    maximum_ego_speed = max(values[1] for values in state.values())
    maximum_command = max(abs(values[0]) for values in command.values()) if command else 0.0
    return {
        "minimum_gap_m": minimum_gap,
        "minimum_gap_at_ns": minimum_gap_at_ns,
        "maximum_ego_speed_mps": maximum_ego_speed,
        "maximum_command_mps2": maximum_command,
    }


def compare_row(
    measured: dict[str, dict[int, list[float]]],
    reference: dict[str, dict[int, list[float]]],
    *,
    constant: bool = False,
) -> dict:
    fields = {"state": CHANNEL_FIELDS["state"]} if constant else CHANNEL_FIELDS
    metrics = compare_observations(measured, reference, fields)
    actual_kpi = kpi_metrics(measured)
    reference_kpi = kpi_metrics(reference)
    metrics.update({
        "minimum_gap_m": actual_kpi["minimum_gap_m"],
        "minimum_gap_at_ns": actual_kpi["minimum_gap_at_ns"],
        "reference_minimum_gap_m": reference_kpi["minimum_gap_m"],
        "minimum_gap_delta_m": actual_kpi["minimum_gap_m"] - reference_kpi["minimum_gap_m"],
        "maximum_ego_speed_mps": actual_kpi["maximum_ego_speed_mps"],
        "reference_maximum_ego_speed_mps": reference_kpi["maximum_ego_speed_mps"],
        "maximum_command_mps2": actual_kpi["maximum_command_mps2"],
        "reference_maximum_command_mps2": reference_kpi["maximum_command_mps2"],
    })
    return metrics


def within_envelope(metrics: dict, envelope: dict | None = None) -> bool:
    envelope = ACCEPTANCE_ENVELOPE if envelope is None else envelope
    for field, error in metrics.get("max_abs_error", {}).items():
        if error > envelope.get(field, math.inf):
            return False
    minimum_gap_delta = abs(metrics.get("minimum_gap_delta_m", 0.0))
    return minimum_gap_delta <= envelope.get("minimum_gap_m", math.inf)


def exceeded_fields(metrics: dict, envelope: dict | None = None) -> list[str]:
    envelope = ACCEPTANCE_ENVELOPE if envelope is None else envelope
    exceeded = [
        field for field, error in metrics.get("max_abs_error", {}).items()
        if error > envelope.get(field, math.inf)
    ]
    if abs(metrics.get("minimum_gap_delta_m", 0.0)) > envelope.get("minimum_gap_m", math.inf):
        exceeded.append("minimum_gap_m")
    return exceeded
