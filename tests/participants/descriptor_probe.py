"""Process participant that lists the descriptors it got when it started.

argv[1] is the report path. The report is a JSON object: each open
descriptor above 2 maps to its kind (fifo, file or other). The participant
writes it before the participant library opens anything, so each entry came
from the process that started this one.
"""

import json
import os
import stat
import sys
from pathlib import Path

# Far above each descriptor that a test Run opens.
HIGHEST_PROBED = 1024


def kind(mode):
    if stat.S_ISFIFO(mode):
        return "fifo"
    return "file" if stat.S_ISREG(mode) else "other"


def inherited_descriptors():
    found = {}
    for fd in range(3, HIGHEST_PROBED):
        try:
            found[fd] = kind(os.fstat(fd).st_mode)
        except OSError:
            continue
    return found


Path(sys.argv[1]).write_text(json.dumps(inherited_descriptors()))

from sil.participant import StepParticipant, run  # noqa: E402


class DescriptorProbe(StepParticipant):
    def on_step(self, t, dt, inputs):
        return []


if __name__ == "__main__":
    run(DescriptorProbe())
