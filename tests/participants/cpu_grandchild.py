"""Process participant whose first Step waits for a CPU-burning grandchild.

The grandchild spins until its own CPU time reaches argv[2] seconds, then
writes that CPU time in nanoseconds to argv[1]. The Participant waits for
it, so the grandchild is a waited descendant of the Participant and of no
other process.
"""

import subprocess
import sys
import time
from pathlib import Path

from sil.participant import StepParticipant, run


def burn(marker: str, seconds: float) -> None:
    while time.process_time() < seconds:
        pass
    Path(marker).write_text(str(time.process_time_ns()))


class CpuGrandchild(StepParticipant):
    def on_step(self, t, dt, inputs):
        if t == 0:
            subprocess.run([sys.executable, __file__, "--burn", *sys.argv[1:]],
                           check=True)
        return []


if __name__ == "__main__":
    if sys.argv[1] == "--burn":
        burn(sys.argv[2], float(sys.argv[3]))
    else:
        run(CpuGrandchild())
