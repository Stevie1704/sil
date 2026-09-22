"""Author the Manifest for one communication-interval experiment row."""
from __future__ import annotations

from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

from sensitivity_contract import (
    CHANNEL_FIELDS,
    INITIAL_LEAD_POSITION_M,
    INITIAL_SENSING,
    INITIAL_SPEED_MPS,
    MANEUVER_FIELDS,
    MANEUVER_PERIOD_NS,
    MIN_GAP_M,
    SensitivityRow,
)

HERE = Path(__file__).resolve().parent


def route_capacity(
    publisher_period_ns: int, subscriber_period_ns: int, latency_ns: int
) -> int:
    """Bound messages that can arrive before a subscriber drains its FIFO."""
    arrivals = (
        latency_ns + subscriber_period_ns + publisher_period_ns - 1
    ) // publisher_period_ns
    return max(2, arrivals + 2)


def _schema(fields: list[str]) -> dict:
    return {"fields": [{"name": field, "type": "f64"} for field in fields]}


def _bindings(channel: str, fields: list[str]) -> list[str]:
    return [
        part
        for field in fields
        for part in ("--bind", f"{channel}:{field}={field}")
    ]


def _plant_closed_loop_binds() -> list[str]:
    return (
        _bindings("command", CHANNEL_FIELDS["command"])
        + _bindings("maneuver", MANEUVER_FIELDS)
        + _bindings("sensing", CHANNEL_FIELDS["sensing"])
        + _bindings("state", CHANNEL_FIELDS["state"])
    )


def _plant_forcing_binds() -> list[str]:
    return _bindings("maneuver", MANEUVER_FIELDS) + _bindings(
        "state", CHANNEL_FIELDS["state"]
    )


def _starts(*, command: float) -> list[str]:
    return [
        "--start", f"accel_mps2={command}",
        "--start", f"initial_lead_position_m={INITIAL_LEAD_POSITION_M}",
    ]


def _controller_starts() -> list[str]:
    return [
        "--start", f"gap_m={INITIAL_SENSING['gap_m']}",
        "--start", f"relative_speed_mps={INITIAL_SENSING['relative_speed_mps']}",
        "--start", f"ego_speed_mps={INITIAL_SPEED_MPS}",
    ]


def _validate_row(row: SensitivityRow) -> None:
    periods = row.periods
    if row.duration_ns % row.plant_period_ns:
        raise ValueError(f"{row.name}: Duration is not a plant-period multiple")
    for participant, period in periods.items():
        if period is not None and row.duration_ns % period:
            raise ValueError(f"{row.name}: Duration is not a {participant}-period multiple")
    latencies = row.latencies
    if row.closed_loop and any(latencies[name] is None for name in ("sensing", "command", "maneuver", "state")):
        raise ValueError(f"{row.name}: closed-loop row lacks a Channel Latency")
    if row.has_maneuver and row.maneuver_period_ns != MANEUVER_PERIOD_NS:
        raise ValueError(f"{row.name}: Maneuver period is not the authored 1 ms grid")


def _add_maneuver(manifest: Manifest) -> None:
    manifest.add_process(
        "maneuver",
        command=["python3", str(HERE / "sensitivity_maneuver.py")],
        step_period_ns=MANEUVER_PERIOD_NS,
        publishes=["maneuver"],
        priority=0,
    )


def manifest_for(row: SensitivityRow) -> Manifest:
    """Build exactly the Manifest declared by one authored row."""
    _validate_row(row)
    manifest = Manifest(duration_ns=row.duration_ns)

    if row.kind == "constant-acceleration":
        manifest.add_schemas({"state": _schema(CHANNEL_FIELDS["state"])})
        manifest.add_channel("state", schema="state", latency_ns=0)
        manifest.add_process(
            "plant",
            command=[
                "python3", "-m", "sil.fmi", "/fmus/AccPlant.fmu",
                *_bindings("state", CHANNEL_FIELDS["state"]),
                *_starts(command=row.initial_command_mps2),
            ],
            step_period_ns=row.plant_period_ns,
            publishes=["state"],
        )
        return manifest

    if row.kind == "changing-input" and not row.closed_loop:
        manifest.add_schemas({
            "maneuver": _schema(MANEUVER_FIELDS),
            "state": _schema(CHANNEL_FIELDS["state"]),
        })
        manifest.add_channel("maneuver", schema="maneuver", latency_ns=0)
        manifest.add_channel("state", schema="state", latency_ns=0)
        _add_maneuver(manifest)
        manifest.add_process(
            "plant",
            command=[
                "python3", "-m", "sil.fmi", "/fmus/AccPlant.fmu",
                *_plant_forcing_binds(),
                *_starts(command=row.initial_command_mps2),
            ],
            step_period_ns=row.plant_period_ns,
            subscribes=[SubscriberRoute(
                "maneuver",
                capacity=route_capacity(
                    MANEUVER_PERIOD_NS, row.plant_period_ns,
                    row.maneuver_latency_ns or 0,
                ),
            )],
            publishes=["state"],
            priority=1,
        )
        return manifest

    manifest.add_schemas({
        name: _schema(fields)
        for name, fields in CHANNEL_FIELDS.items()
        if name != "maneuver" or row.has_maneuver
    })
    for channel, latency in row.latencies.items():
        if latency is not None:
            manifest.add_channel(channel, schema=channel, latency_ns=latency)

    plant_period = row.plant_period_ns
    controller_period = row.controller_period_ns
    maneuver_period = row.maneuver_period_ns
    assert controller_period is not None
    assert maneuver_period is not None
    _add_maneuver(manifest)
    manifest.add_process(
        "plant",
        command=[
            "python3", "-m", "sil.fmi", "/fmus/AccPlant.fmu",
            *_plant_closed_loop_binds(), *_starts(command=row.initial_command_mps2),
        ],
        step_period_ns=plant_period,
        subscribes=[
            SubscriberRoute(
                "command",
                capacity=route_capacity(
                    controller_period, plant_period, row.command_latency_ns or 0,
                ),
            ),
            SubscriberRoute(
                "maneuver",
                capacity=route_capacity(
                    maneuver_period, plant_period, row.maneuver_latency_ns or 0,
                ),
            ),
        ],
        publishes=["sensing", "state"],
        priority=1,
    )
    manifest.add_process(
        "controller",
        command=[
            "python3", "-m", "sil.fmi", "/fmus/AccController.fmu",
            *_bindings("sensing", CHANNEL_FIELDS["sensing"]),
            *_bindings("command", CHANNEL_FIELDS["command"]),
            *_controller_starts(),
        ],
        step_period_ns=controller_period,
        subscribes=[SubscriberRoute(
            "sensing",
            capacity=route_capacity(
                plant_period, controller_period, row.sensing_latency_ns or 0,
            ),
        )],
        publishes=["command"],
        priority=2,
    )
    assert row.kpi_period_ns is not None
    manifest.add_process(
        "kpi",
        command=[
            "python3", str(HERE / "sensitivity_kpi.py"),
            str(MIN_GAP_M), str(row.duration_ns),
        ],
        step_period_ns=row.kpi_period_ns,
        subscribes=[SubscriberRoute(
            "sensing",
            capacity=route_capacity(
                plant_period, row.kpi_period_ns, row.sensing_latency_ns or 0,
            ),
        )],
        priority=3,
    )
    return manifest
