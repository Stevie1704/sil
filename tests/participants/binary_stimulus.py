"""Toy process participant: publishes a bounded binary payload each Step, as
stimulus for the FMI importer's Binary mapping.

The payload is a pure function of the step index (t // dt), so the run stays
deterministic per the participant contract. Its length cycles through every
length the Channel can carry — the empty payload and the full bound included —
and every third byte is zero, so no Message is free of embedded zeros.

The Channel carries the bound; the participant is told the payload field, the
capacity that field declares, and the Channel to publish on. A non-zero
`declared_extra` makes it declare a length its payload field cannot carry —
the publisher the importer has to refuse rather than truncate.
"""

import sys

from sil.participant import StepParticipant, run


def payload(step: int, capacity: int) -> bytes:
    """The payload published at `step`, as bytes."""
    length = step % (capacity + 1)
    return bytes(0 if index % 3 == 0 else (step + index) % 255 + 1
                 for index in range(length))


class BinaryStimulus(StepParticipant):
    def __init__(self, channel: str, field: str, capacity: int,
                 declared_extra: int = 0):
        self.channel = channel
        self.field = field
        self.capacity = capacity
        self.declared_extra = declared_extra

    def on_step(self, t, dt, inputs):
        published = payload(t // dt, self.capacity)
        return [(self.channel, {
            self.field: published.ljust(self.capacity, b"\x00"),
            f"{self.field}_length": len(published) + self.declared_extra,
        })]


if __name__ == "__main__":
    run(BinaryStimulus(sys.argv[1], sys.argv[2], int(sys.argv[3]),
                       int(sys.argv[4])))
