"""Scripted CAN request source: publishes each operation in the Step holding it.

Usage: source.py '[["channel", instant_ns, "hex"], ...]'. The Manifest hashes
the schedule with the command, and each Message states its own FMI event
instant, so the outer Step grid decides only the publication Slot.
"""

import json
import sys

from sil.participant import StepParticipant, run

CAPACITY = 2048


class Source(StepParticipant):
    def __init__(self, schedule):
        self._schedule = [
            (channel, instant, bytes.fromhex(data))
            for channel, instant, data in schedule
        ]

    def on_step(self, t, dt, inputs):
        return [
            (
                channel,
                {
                    "data_length": len(data),
                    "data": data.ljust(CAPACITY, b"\0"),
                    "data_event_time_ns": instant,
                },
            )
            for channel, instant, data in self._schedule
            if t <= instant < t + dt
        ]


if __name__ == "__main__":
    run(Source(json.loads(sys.argv[1])))
