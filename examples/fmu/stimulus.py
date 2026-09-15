"""The example's stimulus: the one participant that is not the FMU.

It publishes the two variables `Feedthrough` declares as inputs. Both are pure
functions of the step index, computed from the kernel's integer `t` and `dt`
on every Step and never accumulated, so the Run is reproducible and every
published value is distinguishable from its neighbours.

This is the participant to replace when the FMU is your own: publish a Channel
whose schema field names are your FMU's input variable names, and the importer
needs nothing else.
"""

from sil.participant import StepParticipant, run

# One triangle over this many Steps. Integer arithmetic decides the shape, so
# the signal is a ramp a reader can follow in the Recording rather than a
# waveform that depends on the platform's libm.
PERIOD_STEPS = 50


class Stimulus(StepParticipant):
    def on_step(self, t, dt, inputs):
        step = t // dt
        phase = step % PERIOD_STEPS
        half = PERIOD_STEPS // 2
        rising = phase if phase < half else PERIOD_STEPS - phase
        return [("fmu.In", {
            "Float64_continuous_input": rising / half,
            "Float64_discrete_input": float(step),
        })]


if __name__ == "__main__":
    run(Stimulus())
