"""Exact-time comparison and independent analytic oracles."""
from __future__ import annotations

import math
import struct

from sil.recording import read_records

from sensitivity_contract import (
    ACCEPTANCE_ENVELOPE,
    CHANNEL_FIELDS,
    INITIAL_LEAD_POSITION_M,
    INITIAL_SPEED_MPS,
    MANEUVER_PERIOD_NS,
    SensitivityRow,
)

# Deliberately repeated as an oracle input schedule. It is integrated here
# rather than calling the FMU's advance() or the Maneuver participant.
_ORACLE_SEGMENTS = (
    (0, 237_000_000, 0.0),
    (237_000_000, 613_000_000, 1.5),
    (613_000_000, 1_087_000_000, -1.5),
    (1_087_000_000, 1_463_000_000, 0.75),
    (1_463_000_000, 2_000_000_000, -0.75),
)


def _oracle_acceleration_at_ns(time_ns: int) -> float:
    for start_ns, end_ns, acceleration in _ORACLE_SEGMENTS:
        if start_ns <= time_ns < end_ns:
            return acceleration
    return _ORACLE_SEGMENTS[-1][2]


def analytic_state(
    time_ns: int,
    acceleration_mps2: float,
    *,
    initial_lead_position_m: float = INITIAL_LEAD_POSITION_M,
) -> dict[str, float]:
    """Independent constant-acceleration state oracle."""
    time_s = time_ns / 1_000_000_000
    ego_position = 25.0 * time_s + 0.5 * acceleration_mps2 * time_s * time_s
    ego_speed = INITIAL_SPEED_MPS + acceleration_mps2 * time_s
    lead_position = initial_lead_position_m + INITIAL_SPEED_MPS * time_s
    return {
        "ego_position_m": ego_position,
        "ego_speed_mps": ego_speed,
        "lead_position_m": lead_position,
        "lead_speed_mps": INITIAL_SPEED_MPS,
        "gap_m": lead_position - ego_position,
        "relative_speed_mps": INITIAL_SPEED_MPS - ego_speed,
    }


def _integrate_oracle_profile(
    time_ns: int, initial_lead_position_m: float,
) -> tuple[float, float]:
    position = initial_lead_position_m
    speed = INITIAL_SPEED_MPS
    cursor = 0
    for start_ns, end_ns, acceleration in _ORACLE_SEGMENTS:
        if cursor >= time_ns:
            break
        begin = max(cursor, start_ns)
        end = min(time_ns, end_ns)
        if end <= begin:
            continue
        duration_s = (end - begin) / 1_000_000_000
        position += speed * duration_s + 0.5 * acceleration * duration_s**2
        speed += acceleration * duration_s
        cursor = end
    return position, speed


def analytic_forcing_state(
    time_ns: int,
    *,
    initial_lead_position_m: float = INITIAL_LEAD_POSITION_M,
) -> dict[str, float]:
    """Continuous piecewise-constant lead-acceleration oracle."""
    lead_position, lead_speed = _integrate_oracle_profile(
        time_ns, initial_lead_position_m,
    )
    # The plant-only refinement holds the ego command at zero.
    ego_position = INITIAL_SPEED_MPS * time_ns / 1_000_000_000
    ego_speed = INITIAL_SPEED_MPS
    return {
        "ego_position_m": ego_position,
        "ego_speed_mps": ego_speed,
        "lead_position_m": lead_position,
        "lead_speed_mps": lead_speed,
        "gap_m": lead_position - ego_position,
        "relative_speed_mps": lead_speed - ego_speed,
    }


def _channel_specs(row: SensitivityRow) -> dict[str, tuple[int, int]]:
    specs = {"state": (row.plant_period_ns, row.plant_period_ns)}
    if row.has_maneuver:
        specs["maneuver"] = (row.maneuver_period_ns or MANEUVER_PERIOD_NS, 0)
    if row.closed_loop:
        specs["sensing"] = (row.plant_period_ns, row.plant_period_ns)
        specs["command"] = (row.controller_period_ns or row.plant_period_ns,
                             row.controller_period_ns or row.plant_period_ns)
    return specs


def _fields_for(row: SensitivityRow) -> dict[str, list[str]]:
    return {channel: CHANNEL_FIELDS[channel] for channel in _channel_specs(row)}


def compare_observations(
    measured: dict[str, dict[int, list[float]]],
    reference: dict[str, dict[int, list[float]]],
    fields: dict[str, list[str]],
) -> dict:
    """Compare native endpoint samples; never synthesize an intermediate one."""
    maximum: dict[str, float] = {
        field: 0.0 for names in fields.values() for field in names
    }
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
                maximum[field] = max(maximum[field], abs(observed - target))
            channel_samples += 1
        if not actual:
            raise RuntimeError(f"no observations for {channel}")
        samples[channel] = channel_samples
    return {"max_abs_error": maximum, "sample_count": samples}


def _expected_times(row: SensitivityRow, channel: str, period_ns: int) -> set[int]:
    if channel == "maneuver":
        return set(range(0, row.duration_ns, period_ns))
    return set(range(period_ns, row.duration_ns + 1, period_ns))


def recording_observations(path, row: SensitivityRow) -> dict[str, dict[int, list[float]]]:
    """Decode native endpoint times from a Recording without resampling it."""
    specs = _channel_specs(row)
    observations = {channel: {} for channel in specs}
    for channel, publish_ns, payload in read_records(path):
        if channel not in specs:
            raise RuntimeError(f"unknown Channel {channel} in sensitivity Recording")
        period_ns, endpoint_offset_ns = specs[channel]
        fields = CHANNEL_FIELDS[channel]
        if len(payload) != 8 * len(fields):
            raise RuntimeError(f"width mismatch for {channel} at {publish_ns} ns")
        endpoint_ns = publish_ns + endpoint_offset_ns
        if endpoint_ns < 0 or endpoint_ns >= row.duration_ns + (channel != "maneuver") * period_ns:
            raise RuntimeError(f"{channel} at {publish_ns} ns is outside the Run")
        if endpoint_ns % row.observation_grid_ns:
            raise RuntimeError(
                f"{channel} at {publish_ns} ns does not land on the exact observation grid"
            )
        if endpoint_ns in observations[channel]:
            raise RuntimeError(f"duplicate exact observation time for {channel}")
        observations[channel][endpoint_ns] = list(
            struct.unpack("<" + "d" * len(fields), payload)
        )
    for channel, (period_ns, _offset) in specs.items():
        wanted = _expected_times(row, channel, period_ns)
        actual = set(observations[channel])
        if actual != wanted:
            raise RuntimeError(
                f"missing/extra exact observations for {channel}: "
                f"expected {sorted(wanted)[:3]}... got {sorted(actual)[:3]}..."
            )
    return observations


def reference_observations(
    document: dict, row: SensitivityRow,
) -> dict[str, dict[int, list[float]]]:
    """Decode the independent driver's endpoint trace on exact integer times."""
    observations = {channel: {} for channel in _channel_specs(row)}
    for channel, messages in document.get("messages", {}).items():
        if channel not in observations:
            raise RuntimeError(f"unknown reference Channel {channel}")
        for message in messages:
            endpoint_ns = message["endpoint_ns"]
            if endpoint_ns % row.observation_grid_ns:
                raise RuntimeError(f"reference endpoint is off-grid: {endpoint_ns} ns")
            if endpoint_ns in observations[channel]:
                raise RuntimeError(f"duplicate reference endpoint for {channel}")
            observations[channel][endpoint_ns] = list(message["values"])
    for channel, (period_ns, _offset) in _channel_specs(row).items():
        expected = _expected_times(row, channel, period_ns)
        if set(observations[channel]) != expected:
            raise RuntimeError(f"reference missing/extra endpoints for {channel}")
    return observations


def _analytic_maneuver(row: SensitivityRow) -> dict[int, list[float]]:
    period_ns = row.maneuver_period_ns or MANEUVER_PERIOD_NS
    return {
        time_ns: [_oracle_acceleration_at_ns(time_ns)]
        for time_ns in range(0, row.duration_ns, period_ns)
    }


def analytic_observations(row: SensitivityRow) -> dict[str, dict[int, list[float]]]:
    period_ns = row.plant_period_ns
    state = {}
    for endpoint_ns in range(period_ns, row.duration_ns + 1, period_ns):
        if row.kind == "constant-acceleration":
            values = analytic_state(
                endpoint_ns, row.initial_command_mps2,
                initial_lead_position_m=INITIAL_LEAD_POSITION_M,
            )
        else:
            values = analytic_forcing_state(
                endpoint_ns,
                initial_lead_position_m=INITIAL_LEAD_POSITION_M,
            )
        state[endpoint_ns] = [values[field] for field in CHANNEL_FIELDS["state"]]
    observations = {"state": state}
    if row.has_maneuver:
        observations["maneuver"] = _analytic_maneuver(row)
    return observations


def kpi_metrics(observations: dict[str, dict[int, list[float]]]) -> dict:
    sensing = observations.get("sensing", {})
    state = observations.get("state", {})
    command = observations.get("command", {})
    gaps = [(values[0], time_ns) for time_ns, values in sensing.items()]
    if not gaps:
        gaps = [(values[4], time_ns) for time_ns, values in state.items()]
    minimum_gap, minimum_gap_at_ns = min(gaps)
    return {
        "minimum_gap_m": minimum_gap,
        "minimum_gap_at_ns": minimum_gap_at_ns,
        "maximum_ego_speed_mps": max(values[1] for values in state.values()),
        "maximum_command_mps2": max(abs(values[0]) for values in command.values()) if command else 0.0,
    }


def compare_row(
    measured: dict[str, dict[int, list[float]]],
    reference: dict[str, dict[int, list[float]]],
    row: SensitivityRow,
) -> dict:
    metrics = compare_observations(measured, reference, _fields_for(row))
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
    return not exceeded_fields(metrics, envelope)


def exceeded_fields(metrics: dict, envelope: dict | None = None) -> list[str]:
    envelope = ACCEPTANCE_ENVELOPE if envelope is None else envelope
    exceeded = [
        field for field, error in metrics.get("max_abs_error", {}).items()
        if error > envelope.get(field, math.inf)
    ]
    if abs(metrics.get("minimum_gap_delta_m", 0.0)) > envelope.get("minimum_gap_m", math.inf):
        exceeded.append("minimum_gap_m")
    return exceeded
