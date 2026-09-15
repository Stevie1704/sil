"""Reject the init line, so a pre-ready failure can be asserted end-to-end."""

from sil.participant import ParticipantFailure, StepParticipant


class RejectAtInit(StepParticipant):
    def on_init(self, init: dict) -> None:
        raise ParticipantFailure("this participant refuses its init line")
