"""Toy process participant: publishes one Float32, Int32, UInt32 and UInt64
each Step, as stimulus for the FMI importer's numeric scalar bindings.

Each Step publishes one row of a fixed table, chosen by the step index
(t // dt), so the Run stays deterministic. The rows hold the boundary values
of each type: the Float32 extremes and its smallest subnormal, the Int32 and
UInt32 limits, and UInt64 values above 2^53, where a route through a double
would lose the low bits.
"""

import sys

from sil.participant import StepParticipant, run

F32_MAX = (2 - 2**-23) * 2**127
F32_TRUE_MIN = 2**-149

ROWS = [
    {"range_m": 1.5, "object_id": -1, "object_count": 0,
     "sample_ns": 0},
    {"range_m": F32_MAX, "object_id": -(2**31), "object_count": 2**32 - 1,
     "sample_ns": 2**64 - 1},
    {"range_m": -F32_MAX, "object_id": 2**31 - 1, "object_count": 1,
     "sample_ns": 2**53 + 1},
    {"range_m": F32_TRUE_MIN, "object_id": 0, "object_count": 2**31,
     "sample_ns": 2**63},
]


class NumericStimulus(StepParticipant):
    def __init__(self, channel: str):
        self.channel = channel

    def on_step(self, t, dt, inputs):
        return [(self.channel, ROWS[(t // dt) % len(ROWS)])]


if __name__ == "__main__":
    run(NumericStimulus(sys.argv[1]))
