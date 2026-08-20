"""Process participant that interleaves equal-time messages on two channels."""

from sil.participant import StepParticipant, run


class MultiChannelBurstSource(StepParticipant):
    """Publish left/right/left/right messages in a deterministic order."""

    def on_step(self, t, dt, inputs):
        """Publish four fixed-size messages at the current virtual time."""
        step = t // dt

        def payload(message_id):
            """Build one fixed-layout payload for the given message id."""
            return {
                "id": message_id,
                "blob": bytes((message_id + k) % 256 for k in range(4)),
                "samples": [float(message_id * 10 + k) for k in range(8)],
            }

        base = step * 4
        return [
            ("left", payload(base)),
            ("right", payload(base + 1)),
            ("left", payload(base + 2)),
            ("right", payload(base + 3)),
        ]


if __name__ == "__main__":
    run(MultiChannelBurstSource())
