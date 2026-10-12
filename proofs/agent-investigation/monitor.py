"""A command monitor of the `monitor-timeout` case (issue #283).

It reads each controller Command and publishes nothing. From 100 ms of
virtual time it waits for a reader of its report pipe, which never comes,
so it stops answering the runner. This is the operational failure of the
proof: the Run fails on the monitor's response deadline, and no behavioral
verdict about the controller exists.
"""

import os

from sil.participant import StepParticipant, run

FLUSH_AT_NS = 100_000_000
PIPE = "monitor-report.fifo"


class Monitor(StepParticipant):
    def on_step(self, t, dt, inputs):
        if t >= FLUSH_AT_NS:
            if not os.path.exists(PIPE):
                os.mkfifo(PIPE)
            # Opening a FIFO for writing blocks until a reader opens it.
            with open(PIPE, "w") as report:
                report.write(f"{len(inputs)}\n")


if __name__ == "__main__":
    run(Monitor())
