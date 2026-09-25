"""Test participant that changes a Replay's Recording file at its first step,
after the Replayer has validated it (issue #180).

argv: <mode> <recording> [<replacement>]
  replace   renames <replacement> over <recording>: the path now names a new file
  overwrite writes zeros into the second half of <recording> in place and moves
            its modification time, so the change is visible to fstat
"""

import os
import sys

from sil.participant import StepParticipant, run


class RecordingMutator(StepParticipant):
    def __init__(self, mode, recording, replacement=None):
        self.mode = mode
        self.recording = recording
        self.replacement = replacement
        self.done = False

    def on_step(self, t, dt, inputs):
        if self.done:
            return []
        self.done = True
        if self.mode == "replace":
            os.replace(self.replacement, self.recording)
        else:
            before = os.stat(self.recording)
            with open(self.recording, "r+b") as f:
                f.seek(before.st_size // 2)
                f.write(bytes(before.st_size // 4))
            os.utime(self.recording,
                     ns=(before.st_atime_ns, before.st_mtime_ns - 10**9))
        return []


if __name__ == "__main__":
    run(RecordingMutator(*sys.argv[1:]))
