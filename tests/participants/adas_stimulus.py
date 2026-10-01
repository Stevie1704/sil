"""Toy process participant: live radar, camera and ego-speed stimulus for the
ADAS reference controller, with one field overridden at one Step.

    adas_stimulus.py <prefix> [<sensor>.<field>=<value>@<step> ...]

Each Step publishes a confirmed, non-hazardous object sampled at t on
`<prefix>.radar`, `<prefix>.camera` and `<prefix>.ego`. An override replaces
one field at one Step, which is how the tests deliver values `sil-csv` refuses
to convert, such as NaN, and impossible Sample times.
"""

import sys

from sil.participant import StepParticipant, run


def _value(text: str):
    return float(text) if any(c in text for c in ".naife") else int(text)


class AdasStimulus(StepParticipant):
    def __init__(self, prefix: str, overrides: list[str]):
        self.prefix = prefix
        self.overrides = {}
        for item in overrides:
            target, step = item.rsplit("@", 1)
            name, value = target.split("=", 1)
            sensor, field = name.split(".", 1)
            self.overrides[(int(step), sensor, field)] = _value(value)

    def on_step(self, t, dt, inputs):
        step = t // dt
        messages = {
            "radar": {"sample_time_ns": t, "sequence": step, "sensor_id": 1,
                      "object_id": 7, "x_m": 40.0, "y_m": 0.0,
                      "relative_vx_mps": 0.0, "confidence": 0.875},
            "camera": {"sample_time_ns": t, "sequence": step, "sensor_id": 2,
                       "object_id": 3, "x_m": 40.5, "y_m": 0.25,
                       "confidence": 0.75},
            "ego": {"sample_time_ns": t, "sequence": step, "speed_mps": 20.0},
        }
        for (at, sensor, field), value in self.overrides.items():
            if at == step:
                messages[sensor][field] = value
        return [(f"{self.prefix}.{sensor}", fields)
                for sensor, fields in messages.items()]


if __name__ == "__main__":
    run(AdasStimulus(sys.argv[1], sys.argv[2:]))
