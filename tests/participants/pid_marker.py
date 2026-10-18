"""Process participant that writes its pid when spawned, then ends one way.

argv[1] is the marker path. argv[2] is how the Participant ends: `ok` steps
to the end of the Run, `fail` fails its second Step, and `hang` never answers
its first Step, so only a participant timeout ends it.
"""

import os
import sys
import time
from pathlib import Path

from sil.participant import StepParticipant, run

Path(sys.argv[1]).write_text(str(os.getpid()))
MODE = sys.argv[2]


class PidMarker(StepParticipant):
    def on_step(self, t, dt, inputs):
        if MODE == "hang":
            while True:
                time.sleep(1)
        assert MODE != "fail" or t == 0, f"fails at t={t}"
        return []


if __name__ == "__main__":
    run(PidMarker())
