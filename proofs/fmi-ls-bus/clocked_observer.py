"""The consumer side of the clocked CAN frame Channel.

A published Channel with no subscriber describes a Run nobody would execute.
What a consumer of a bus node wants is the frames it sent, and each one it is
handed carries both times the Channel distinguishes: the Virtual time it
became visible at, and the FMI event time the Message states it was produced
at.

Reports go to stderr: stdout is the step protocol.
"""

import sys

from sil.participant import StepParticipant, run


class ClockedObserver(StepParticipant):
    def on_step(self, t, dt, inputs):
        for message in inputs:
            length = message.data["data_length"]
            print(
                f"observer: visible {t} ns  event "
                f"{message.data['data_event_time_ns']} ns  "
                f"{message.data['data'][:length].hex()}",
                file=sys.stderr, flush=True,
            )
        return []


if __name__ == "__main__":
    run(ClockedObserver())
