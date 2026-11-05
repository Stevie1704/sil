"""Process participant that loads an instrumented library (#372, Q17).

It calls `scope_scenarios_probe` of tests/fixtures/participants/
scope_scenarios.c, which runs source scopes, and publishes the result as
toy.Counter. It inherits SIL_SOURCE_SCOPE_SESSION, but its PID is not the
runner's: its observer must stay dormant.
"""

import ctypes
import sys

from sil.participant import StepParticipant, run


class ScopeProcess(StepParticipant):
    def __init__(self, library: str, channel: str):
        self.probe = ctypes.CDLL(library).scope_scenarios_probe
        self.channel = channel

    def on_step(self, t, dt, inputs):
        step = t // dt
        return [(self.channel, {"seq": step, "value": self.probe(2)})]


if __name__ == "__main__":
    run(ScopeProcess(sys.argv[1], sys.argv[2]))
