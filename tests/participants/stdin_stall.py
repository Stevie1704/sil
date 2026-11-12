"""Raw step-protocol participants for the Step write deadline tests (#386).

Usage: stdin_stall.py <mode> [marker]

Modes:
  source        publish one BLOB_BYTES inline Message on "blob" every Step
  stop-reading  answer `ready` and the first Step, write the marker file,
                then stop reading stdin for STALL_SECONDS
  stderr-flood  after the first Step, write more than a pipe buffer to
                stderr before it reads stdin again, then answer normally
"""

import base64
import json
import sys
import time
from pathlib import Path

# Larger than the default pipe buffer (64 KiB on Linux and macOS), so the
# Step line that carries it cannot go into the pipe in one write.
BLOB_BYTES = 300_000
STDERR_FLOOD_BYTES = 256 * 1024
# Bounded, so a runner that a failed test kills leaves no orphan behind.
STALL_SECONDS = 60
FLOOD_DONE = "stderr flood done"


def answer(out: list) -> None:
    print(json.dumps({"op": "step_done", "out": out}), flush=True)


def main(mode: str, marker: str | None) -> None:
    blob = base64.b64encode(bytes(BLOB_BYTES)).decode()
    steps = 0
    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            print(json.dumps({"op": "ready"}), flush=True)
        elif message["op"] == "step":
            answer([{"ch": "blob", "data": blob}] if mode == "source" else [])
            steps += 1
            if steps == 1 and mode == "stop-reading":
                Path(marker).write_text("stopped reading\n")
                time.sleep(STALL_SECONDS)
                return
            if steps == 1 and mode == "stderr-flood":
                sys.stderr.write("x" * STDERR_FLOOD_BYTES + "\n")
                sys.stderr.write(FLOOD_DONE + "\n")
                sys.stderr.flush()
        elif message["op"] == "shutdown":
            return


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
