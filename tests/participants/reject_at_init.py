"""Two ways to fail before `ready`, which the kernel tells apart.

`RejectAtInit` says its init line cannot be honoured; `BreakAtInit` merely
breaks on the way up. The first is a Manifest error, the second a Run failure.
"""

from sil.participant import ManifestError, ParticipantFailure, StepParticipant


class RejectAtInit(StepParticipant):
    def on_init(self, init: dict) -> None:
        raise ManifestError("this participant refuses its init line")


class BreakAtInit(StepParticipant):
    def on_init(self, init: dict) -> None:
        raise RuntimeError("this participant broke while initializing")


class FailAtInit(StepParticipant):
    def on_init(self, init: dict) -> None:
        raise ParticipantFailure("the participant's own initialization failed")
