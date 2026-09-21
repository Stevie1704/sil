"""Toy process participant: publishes one CAN frame per Step.

It is the sending half of the clocked importer's Channel contract — each
Message it publishes is one activation of the imported node's input Clock. The
payload is a pure function of the step index, so the Run stays deterministic
per the participant contract.

The event time it states is its own publication time, which the importer does
not use: an activation happens at the communication point the FMU stands on,
and that is what the echoed Message states.
"""

import sys

from sil.participant import StepParticipant, run

# The bound the node's Binary variables declare.
CAN_BUFFER_BYTES = 2048


def payload(step: int) -> bytes:
    """The frame published at `step`, as bytes."""
    return bytes((step + index) % 251 + 1 for index in range(1 + step % 4))


class CanStimulus(StepParticipant):
    def __init__(self, channel: str):
        self.channel = channel

    def on_step(self, t, dt, inputs):
        frame = payload(t // dt)
        return [(self.channel, {
            "data": frame.ljust(CAN_BUFFER_BYTES, b"\x00"),
            "data_length": len(frame),
            "data_event_time_ns": t,
        })]


if __name__ == "__main__":
    run(CanStimulus(sys.argv[1]))
