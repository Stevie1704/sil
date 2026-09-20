"""The consumer-side reader of the CAN frame Channel.

It exists so the `binary-channel` Manifest describes a Run someone would
actually execute: a published Channel needs a subscriber, and what a consumer
wants from a CAN node is the frames it sent. Each non-empty buffer it is
handed is reported with the Virtual time it became visible at.

The Run this participant belongs to does not currently reach a Step. The
importer rejects the Manifest before that, which is the evidence the proof
retains — this participant is what makes the rejection a statement about the
importer rather than about a Manifest nobody would write.

Reports go to stderr: stdout is the step protocol.
"""

import sys

from sil.participant import StepParticipant, run


class Observer(StepParticipant):
    def on_step(self, t, dt, inputs):
        for message in inputs:
            length = message.data["CanChannel.Tx_Data_length"]
            if length:
                payload = message.data["CanChannel.Tx_Data"][:length]
                print(f"observer: {t} ns {payload.hex()}", file=sys.stderr,
                      flush=True)
        return []


if __name__ == "__main__":
    run(Observer())
