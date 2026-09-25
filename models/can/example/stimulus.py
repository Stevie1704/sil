"""Example CAN nodes: publish each compiled request in the Step that holds it.

Usage: stimulus.py '[["channel", instant_ns, "hex"], ...]' CAPACITY. The schedule is
part of the command, so the Manifest hash covers it, and each Message states
its own FMI event instant; the Step grid only chooses the publication Slot.
"""

import json
import sys

from sil.participant import StepParticipant, run


class Nodes(StepParticipant):
    def __init__(self, schedule, capacity):
        self._capacity = capacity
        self._schedule = [
            (channel, instant, bytes.fromhex(data))
            for channel, instant, data in schedule
        ]

    def on_step(self, t, dt, inputs):
        return [
            (channel, {
                "data_length": len(data),
                "data": data.ljust(self._capacity, b"\0"),
                "data_event_time_ns": instant,
            })
            for channel, instant, data in self._schedule
            if t <= instant < t + dt
        ]


if __name__ == "__main__":
    run(Nodes(json.loads(sys.argv[1]), int(sys.argv[2])))
