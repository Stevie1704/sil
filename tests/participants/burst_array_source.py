"""Process participant that publishes two fixed-size array messages per step."""

from sil.participant import StepParticipant, run


class BurstArraySource(StepParticipant):
    """Publish two messages at each step to exercise recording tie-breaks."""

    def on_step(self, t, dt, inputs):
        """
        Publish two deterministic payloads for the current simulation step.
        
        Parameters:
        	t (int or float): Current virtual simulation time.
        	dt (int or float): Duration of one simulation step.
        	inputs: Step inputs.
        
        Returns:
        	list: Two ``("payload", payload)`` message tuples with consecutive message IDs.
        """
        step = t // dt

        def payload(message_id):
            """Build one fixed-layout payload for the given message id."""
            return {
                "id": message_id,
                "blob": bytes((message_id + k) % 256 for k in range(4)),
                "samples": [float(message_id * 10 + k) for k in range(8)],
            }

        return [("payload", payload(step * 2)), ("payload", payload(step * 2 + 1))]


if __name__ == "__main__":
    run(BurstArraySource())
