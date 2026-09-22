"""Manifest authoring for one row of the communication-interval study."""
from __future__ import annotations

from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

from sensitivity_contract import (
    CHANNEL_FIELDS,
    MIN_GAP_M,
    OBSERVATION_GRID_NS,
)

HERE = Path(__file__).resolve().parent


def route_capacity(publisher_period_ns: int, subscriber_period_ns: int,
                   latency_ns: int) -> int:
    """Bound messages that can arrive before a subscriber drains its FIFO."""
    arrivals = (latency_ns + subscriber_period_ns + publisher_period_ns - 1) // publisher_period_ns
    return max(2, arrivals + 2)


def _schema(fields: list[str]) -> dict:
    return {"fields": [{"name": field, "type": "f64"} for field in fields]}


def _bindings(channel: str, fields: list[str]) -> list[str]:
    return [part for field in fields for part in ("--bind", f"{channel}:{field}={field}")]


def _plant_binds() -> list[str]:
    return _bindings("sensing", CHANNEL_FIELDS["sensing"]) + _bindings(
        "state", CHANNEL_FIELDS["state"]
    )


def _validate_row(row: dict) -> None:
    periods = row["periods_ns"]
    duration = row["duration_ns"]
    plant_period = periods["plant"]
    if plant_period is None or duration % plant_period:
        raise ValueError(f"{row['name']}: Duration is not a plant-period multiple")
    if row["scenario"] == "constant-acceleration":
        if any(value is not None for value in (periods["controller"], periods["kpi"])):
            raise ValueError(f"{row['name']}: constant row unexpectedly has a controller")
        return
    if periods["controller"] is None or periods["kpi"] is None:
        raise ValueError(f"{row['name']}: changing-input row lacks a participant period")
    for participant, period in periods.items():
        if duration % period:
            raise ValueError(f"{row['name']}: Duration is not a {participant}-period multiple")
    latencies = row["latencies_ns"]
    if any(value is None for value in latencies.values()):
        raise ValueError(f"{row['name']}: changing-input row lacks a Channel Latency")
    if row["observation_grid_ns"] != OBSERVATION_GRID_NS:
        raise ValueError(f"{row['name']}: unexpected observation grid")


def manifest_for(row: dict) -> Manifest:
    """Build exactly the Manifest described by one pre-authored row."""
    _validate_row(row)
    periods = row["periods_ns"]
    latencies = row["latencies_ns"]
    manifest = Manifest(duration_ns=row["duration_ns"])

    if row["scenario"] == "constant-acceleration":
        manifest.add_schemas({"state": _schema(CHANNEL_FIELDS["state"])})
        manifest.add_channel("state", schema="state", latency_ns=0)
        manifest.add_process(
            "plant",
            command=[
                "python3", "-m", "sil.fmi", "/fmus/AccPlant.fmu",
                *_plant_binds(), "--start",
                f"accel_mps2={row['initial_command_mps2']}",
            ],
            step_period_ns=periods["plant"],
            publishes=["state"],
        )
        return manifest

    manifest.add_schemas({name: _schema(fields) for name, fields in CHANNEL_FIELDS.items()})
    for channel in CHANNEL_FIELDS:
        manifest.add_channel(channel, schema=channel, latency_ns=latencies[channel])

    plant_period = periods["plant"]
    controller_period = periods["controller"]
    kpi_period = periods["kpi"]
    plant_command_capacity = route_capacity(
        controller_period, plant_period, latencies["command"]
    )
    controller_sensing_capacity = route_capacity(
        plant_period, controller_period, latencies["sensing"]
    )
    kpi_sensing_capacity = route_capacity(
        plant_period, kpi_period, latencies["sensing"]
    )
    manifest.add_process(
        "plant",
        command=[
            "python3", "-m", "sil.fmi", "/fmus/AccPlant.fmu",
            *_plant_binds(), "--start", "accel_mps2=0.0",
        ],
        step_period_ns=plant_period,
        subscribes=[SubscriberRoute("command", capacity=plant_command_capacity)],
        publishes=["sensing", "state"],
        priority=0,
    )
    manifest.add_process(
        "controller",
        command=[
            "python3", "-m", "sil.fmi", "/fmus/AccController.fmu",
            *_bindings("sensing", CHANNEL_FIELDS["sensing"]),
            *_bindings("command", CHANNEL_FIELDS["command"]),
        ],
        step_period_ns=controller_period,
        subscribes=[SubscriberRoute("sensing", capacity=controller_sensing_capacity)],
        publishes=["command"],
        priority=1,
    )
    manifest.add_process(
        "kpi",
        command=[
            "python3", str(HERE / "sensitivity_kpi.py"), str(MIN_GAP_M),
        ],
        step_period_ns=kpi_period,
        subscribes=[SubscriberRoute("sensing", capacity=kpi_sensing_capacity)],
        priority=2,
    )
    return manifest
