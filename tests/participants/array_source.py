"""Large-payload source participant: publishes a fixed-size array message
each step so the array wire layout can be round-tripped bit-for-bit over the
inline transport.

The payload is a pure function of the step index (t // dt), keeping the run
deterministic per the participant contract (no wall-clock, derived from t).
"""

from sil.participant import StepParticipant, run


class ArraySource(StepParticipant):
    def on_step(self, t, dt, inputs):
        step = t // dt
        samples = [float(step * 10 + k) for k in range(8)]
        blob = bytes((step + k) % 256 for k in range(4))
        return [("payload", {"id": step, "blob": blob, "samples": samples})]


if __name__ == "__main__":
    run(ArraySource())
