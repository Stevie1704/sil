"""Two ways to fail before `ready`, which the kernel tells apart.

`RejectAtInit` says its init line cannot be honoured; `BreakAtInit` merely
breaks on the way up. The first is a configuration error, the second a Run
failure.
"""

from sil.participant import ConfigurationError, StepParticipant


class RejectAtInit(StepParticipant):
    def on_init(self, init: dict) -> None:
        raise ConfigurationError("this participant refuses its init line")


class BreakAtInit(StepParticipant):
    def on_init(self, init: dict) -> None:
        raise RuntimeError("this participant broke while initializing")
