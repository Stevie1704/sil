"""Authored maneuvers and thresholds, fixed before executing the evidence gate."""
import json
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

HERE = Path(__file__).resolve().parent
STEP_NS = 10_000_000
FIELDS = {
    "sensing": ["gap_m", "relative_speed_mps", "ego_speed_mps"],
    "truth": ["gap_m", "relative_speed_mps", "ego_speed_mps",
              "ego_position_m", "lead_position_m", "lead_speed_mps"],
    "command": ["accel_mps2"],
    "maneuver": ["lead_accel_mps2"],
    "freshness": ["age_ns"],
}


def configuration(name):
    """All physical inputs and KPI policy are also embedded in the Manifest."""
    cases = {
        "nominal": ("close the gap and leave upper saturation", 0.0, None, 0),
        "braking": ("finite lead braking reaches lower saturation and recovers", -4.0, None, 0),
        "delayed": ("delayed sensing retains bounded tracking", -4.0, "delay", 0),
        "dropped": ("held sensing during braking eventually violates minimum gap", -4.0, "drop", 1),
    }
    behavior, braking, fault, exit_code = cases[name]
    return dict(name=name, behavior=behavior, expected_exit=exit_code,
                step_ns=STEP_NS, steps=2000, duration_ns=20_000_000_000,
                initial=dict(ego_position_m=0.0, lead_position_m=60.0,
                             ego_speed_mps=25.0, lead_speed_mps=25.0, accel_mps2=0.0),
                rates_hz=dict(sensing=100, command=100, maneuver=100, kpi=100),
                units=dict(position="m", gap="m", speed="m/s", acceleration="m/s2", time="ns"),
                maneuver=dict(start_ns=2_000_000_000, end_ns=5_000_000_000,
                              lead_accel_mps2=braking),
                fault=None if fault is None else dict(kind=fault, start_ns=2_000_000_000,
                    end_ns=15_000_000_000 if fault == "drop" else 4_000_000_000,
                    delay_ns=200_000_000 if fault == "delay" else 0),
                minimum_observable_hold_ns=200_000_000 if fault == "delay" else 1_000_000_000,
                kpi=dict(minimum_gap_m=5.0, acceleration_min_mps2=-3.0,
                         acceleration_max_mps2=1.5, tracking_start_ns=15_000_000_000,
                         spacing_error_max_m=3.0, relative_speed_max_mps=1.0),
                zero_speed_policy="spacing error = gap - (5 + 1.5 * max(ego speed, 0)); no headway/TTC division",
                absolute_tolerance=1e-10, relative_tolerance=1e-12)


NAMES = ("nominal", "braking", "delayed", "dropped")


def manifest(config):
    m = Manifest(duration_ns=config["duration_ns"])
    for channel, names in FIELDS.items():
        m.add_schemas({channel: {"fields": [{"name": n, "type": "f64"} for n in names]}})
        m.add_channel(channel, schema=channel, latency_ns=0 if channel == "maneuver" else STEP_NS)
    fault = config["fault"]
    if fault:
        args = {k: v for k, v in fault.items() if k != "delay_ns" or fault["kind"] == "delay"}
        m.add_interceptor("sensing", **args)
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"))
    m.add_process("maneuver", command=["python3", str(HERE / "scenario_participant.py"), "stimulus", encoded],
                  step_period_ns=STEP_NS, publishes=["maneuver"], priority=-1)
    for model, name, incoming, outgoing, priority in (
        ("AccPlant", "plant", ["command", "maneuver"], ["sensing", "truth"], 0),
        ("AccController", "controller", ["sensing"], ["command"], 1),
    ):
        binds = [part for ch in incoming + outgoing for field in FIELDS[ch]
                 for part in ("--bind", f"{ch}:{field}={field}")]
        m.add_process(name, command=["python3", "-m", "sil.fmi", f"/fmus/{model}.fmu", *binds],
                      step_period_ns=STEP_NS, priority=priority, publishes=outgoing,
                      subscribes=[SubscriberRoute(ch, capacity=64) for ch in incoming])
    m.add_process("kpi", command=["python3", str(HERE / "scenario_participant.py"), "kpi", encoded],
                  step_period_ns=STEP_NS, priority=2, publishes=["freshness"],
                  subscribes=[SubscriberRoute(ch, capacity=64) for ch in ("truth", "sensing", "command")])
    return m
