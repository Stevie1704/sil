"""Large-payload sink: subscribes to an array channel and republishes each
received message verbatim onto a second channel.

Used to exercise the kernel->child shm *input* path (the kernel writes the
payload into the arena, this participant reads it) — the array_source fixture
only covers the child->kernel output path. The republished field-dict is the
one on_step received, so the mirror recording proves the input bytes survived.
"""

from sil.participant import StepParticipant, run


class ArrayEcho(StepParticipant):
    def on_step(self, t, dt, inputs):
        return [("mirror", i.data) for i in inputs]


if __name__ == "__main__":
    run(ArrayEcho())
