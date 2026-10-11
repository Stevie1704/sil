"""Toy process participant: live radar and camera object lists and ego
motion for the ADAS reference controller, with fields overridden at one Step.

    adas_stimulus.py <prefix> [<override> ...]

Each Step publishes, sampled at t, a radar list of two objects and a camera
list of one object that confirms radar object 7 (not a hazard) on
`<prefix>.radar` and `<prefix>.camera`, and the ego speed on `<prefix>.ego`.
An override is one of:

- `<sensor>.<field>=<value>@<step>` replaces one header field;
- `<sensor>.<field>[<i>]=<value>@<step>` replaces one array element;
- `<sensor>*<n>@<step>` publishes that sensor's Message n times.

This is how the tests deliver what `sil recording csv` refuses to convert or
`prepare.py` refuses to write, such as NaN, a count above the capacity,
nonzero inactive elements and impossible Sample times.
"""

import re
import sys

from sil.participant import StepParticipant, run

CAPACITY = 8
_FIELD = re.compile(r"(\w+)\.(\w+)(?:\[(\d+)\])?=(.+)@(\d+)")
_REPEAT = re.compile(r"(\w+)\*(\d+)@(\d+)")


def _value(text: str):
    return float(text) if any(c in text for c in ".naife") else int(text)


def _objects(sensor_id: int, t: int, step: int, objects: list[tuple]) -> dict:
    """The flat Schema form: active objects first, inactive elements zero."""
    padded = objects + [(0, 0.0, 0.0, 0.0, 0.0)] * (CAPACITY - len(objects))
    ids, xs, ys, vxs, confidences = (list(column) for column in zip(*padded))
    return {"sample_time_ns": t, "sensor_id": sensor_id, "frame_id": 1,
            "sequence": step, "count": len(objects), "validity": 1,
            "object_id": ids, "x_m": xs, "y_m": ys, "relative_vx_mps": vxs,
            "confidence": confidences}


class AdasStimulus(StepParticipant):
    def __init__(self, prefix: str, overrides: list[str]):
        self.prefix = prefix
        self.fields = []
        self.repeats = {}
        for item in overrides:
            if m := _REPEAT.fullmatch(item):
                self.repeats[(int(m[3]), m[1])] = int(m[2])
            elif m := _FIELD.fullmatch(item):
                index = None if m[3] is None else int(m[3])
                self.fields.append((int(m[5]), m[1], m[2], index, _value(m[4])))
            else:
                raise SystemExit(f"adas_stimulus: bad override {item!r}")

    def on_step(self, t, dt, inputs):
        step = t // dt
        messages = {
            "radar": _objects(1, t, step, [(7, 40.0, 0.0, 0.0, 0.875),
                                           (8, 60.0, 0.5, 0.0, 0.875)]),
            "camera": _objects(2, t, step, [(3, 40.5, 0.25, 0.0, 0.75)]),
            "ego": {"sample_time_ns": t, "sequence": step, "validity": 1,
                    "speed_mps": 20.0},
        }
        for at, sensor, field, index, value in self.fields:
            if at != step:
                continue
            if index is None:
                messages[sensor][field] = value
            else:
                messages[sensor][field][index] = value
        return [(f"{self.prefix}.{sensor}", fields)
                for sensor, fields in messages.items()
                for _ in range(self.repeats.get((step, sensor), 1))]


if __name__ == "__main__":
    run(AdasStimulus(sys.argv[1], sys.argv[2:]))
