#!/usr/bin/env python3
"""Step-protocol probe that exposes and dirties its inherited directory."""

import json
import os
import signal
import sys
from pathlib import Path


observer = Path(sys.argv[1])
observer.joinpath("cwd").write_text(str(Path.cwd()))
Path("participant-owned.txt").write_text("remove me with the working directory")

for line in sys.stdin:
    message = json.loads(line)
    if message["op"] == "init":
        print(json.dumps({"op": "ready"}), flush=True)
    elif message["op"] == "step":
        if len(sys.argv) > 2 and sys.argv[2] == "sigkill":
            os.kill(os.getpid(), signal.SIGKILL)
        print(json.dumps({"op": "step_done", "out": []}), flush=True)
    elif message["op"] == "shutdown":
        break
