"""Process participant that writes its PID and its preload environment.

argv[1] is the marker path. The marker is JSON: `pid`, `ld_preload` and
`dump_heaptrack_output` (null when unset), so a test can tell which process
a profiler adapter reached, and `environment`, the whole environment, so a
test can compare it with that of an ordinary Run.
"""

import json
import os
import sys
from pathlib import Path

from sil.participant import StepParticipant, run

Path(sys.argv[1]).write_text(json.dumps({
    "pid": os.getpid(),
    "ld_preload": os.environ.get("LD_PRELOAD"),
    "dump_heaptrack_output": os.environ.get("DUMP_HEAPTRACK_OUTPUT"),
    "environment": dict(os.environ),
}))


class EnvMarker(StepParticipant):
    def on_step(self, t, dt, inputs):
        return []


if __name__ == "__main__":
    run(EnvMarker())
