"""Test participants: explicit maneuver and in-run physical KPIs/freshness."""
import json
import math
import sys

from sil.participant import StepParticipant, run


def violations(channel, data, publication_ns, config):
    k = config["kpi"]
    errors = []

    def check(field, value, low, high):
        if not math.isfinite(value) or not low <= value <= high:
            errors.append(f"ACC KPI {field} publication_ns={publication_ns} value={value} bounds=[{low},{high}]")

    for field, value in data.items():
        if not math.isfinite(value):
            errors.append(f"ACC KPI {field} publication_ns={publication_ns} value={value} threshold=finite")
    if channel == "truth":
        check("gap_m", data["gap_m"], k["minimum_gap_m"], math.inf)
        check("ego_speed_mps", data["ego_speed_mps"], 0, math.inf)
        check("lead_speed_mps", data["lead_speed_mps"], 0, math.inf)
        if publication_ns >= k["tracking_start_ns"]:
            error = data["gap_m"] - (5 + 1.5 * max(data["ego_speed_mps"], 0))
            check("spacing_error_m", error, -k["spacing_error_max_m"], k["spacing_error_max_m"])
            check("relative_speed_mps", data["relative_speed_mps"],
                  -k["relative_speed_max_mps"], k["relative_speed_max_mps"])
    elif channel == "command":
        check("accel_mps2", data["accel_mps2"], k["acceleration_min_mps2"], k["acceleration_max_mps2"])
    return errors


class Stimulus(StepParticipant):
    def __init__(self, config):
        self.maneuver = config["maneuver"]

    def on_step(self, t, dt, inputs):
        m = self.maneuver
        value = m["lead_accel_mps2"] if m["start_ns"] <= t < m["end_ns"] else 0.0
        return [("maneuver", {"lead_accel_mps2": value})]


class ScenarioKPI(StepParticipant):
    def __init__(self, config):
        self.config = config
        self.last_delivery_ns = 0
        self.checked = dict(truth=0, command=0)

    def on_step(self, t, dt, inputs):
        for message in inputs:
            if message.channel == "sensing":
                self.last_delivery_ns = t
            else:
                errors = violations(message.channel, message.data, message.publish_ns, self.config)
                if errors:
                    raise RuntimeError(errors[0])
                self.checked[message.channel] += 1
        if t == self.config["duration_ns"] - self.config["step_ns"]:
            print("ACC_SCENARIO_KPI " + json.dumps(self.checked), file=sys.stderr)
        # Time since last delivery, not source sample age. The independent
        # trace additionally records exactly which plant state was sampled.
        return [("freshness", {"age_ns": float(t - self.last_delivery_ns)})]


if __name__ == "__main__":
    config = json.loads(sys.argv[2])
    run(Stimulus(config) if sys.argv[1] == "stimulus" else ScenarioKPI(config))
