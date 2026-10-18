"""Toy participant that holds MIB mebibytes resident during one Step.

    memory_peak.py PEAK_AT_NS MIB

At virtual time PEAK_AT_NS it allocates and touches MIB MiB, then frees them
before it answers, so its resident peak is short and at a known time.
"""

import sys

from sil.participant import StepParticipant, run

PAGE = 4096


class MemoryPeak(StepParticipant):
    def __init__(self, peak_at_ns: int, mib: int):
        self.peak_at_ns = peak_at_ns
        self.size = mib * 1024 * 1024

    def on_step(self, t, dt, inputs):
        if t == self.peak_at_ns:
            block = bytearray(self.size)
            for offset in range(0, self.size, PAGE):
                block[offset] = 1
            del block
        return []


if __name__ == "__main__":
    run(MemoryPeak(int(sys.argv[1]), int(sys.argv[2])))
