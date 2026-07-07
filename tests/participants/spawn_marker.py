"""Process participant that writes a marker file the instant it is spawned.

Used to prove the kernel started (or did not start) participants: the marker
exists iff the kernel actually launched this process. The path is argv[1].
"""

import sys
from pathlib import Path

from sil.participant import StepParticipant, run

Path(sys.argv[1]).write_text("spawned")


class SpawnMarker(StepParticipant):
    def on_step(self, t, dt, inputs):
        return []


if __name__ == "__main__":
    run(SpawnMarker())
