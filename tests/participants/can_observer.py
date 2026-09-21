"""Toy process participant: the consumer side of a clocked CAN Channel.

A published Channel with no subscriber describes a Run nobody would execute,
and what a consumer of a bus node's frames does is read them. Each activation
it is handed is reported with both times the Channel distinguishes: the
Virtual time it became visible at, and the FMI event time the Message states.

Reports go to stderr; stdout is the step protocol.
"""

import sys

from sil.participant import StepParticipant, run


class CanObserver(StepParticipant):
    def __init__(self, channel: str):
        self.channel = channel

    def on_step(self, t, dt, inputs):
        for message in inputs:
            length = message.data["data_length"]
            print(
                f"observer: {t} ns event {message.data['data_event_time_ns']} "
                f"ns {message.data['data'][:length].hex()}",
                file=sys.stderr, flush=True,
            )
        return []


if __name__ == "__main__":
    run(CanObserver(sys.argv[1]))
