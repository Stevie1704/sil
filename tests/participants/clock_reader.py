"""Toy clock-reading process participant for the run-boundary shim suite.

It reads the POSIX clock APIs the shim interposes and republishes what it
observed, so the run-boundary tests can assert the recorded readings equal the
step's virtual time (monotonic) and the declared epoch plus virtual time
(realtime). It reads *only* the interposed functions — clock_gettime and
time — deliberately: time.monotonic() on macOS uses mach_absolute_time(), which
the shim does not (and by the PRD's scope cannot) interpose.

Published on toy.Counter: seq = monotonic ns, value = realtime ns.
"""

import time

from sil.participant import StepParticipant, run


class ClockReader(StepParticipant):
    def on_step(self, t, dt, inputs):
        monotonic = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        realtime = time.clock_gettime_ns(time.CLOCK_REALTIME)
        return [("readings", {"seq": monotonic, "value": realtime})]


if __name__ == "__main__":
    run(ClockReader())
