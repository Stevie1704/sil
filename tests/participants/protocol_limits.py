"""Raw Step-protocol participant fixtures for run-boundary limit tests."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


PAYLOAD = base64.b64encode(b"\0" * 16).decode()

# The cheapest legal `out` entry, so a line of a given size carries as many
# JSON nodes as a participant can put in it. That is the worst case for what
# the kernel parses before the output-Message guard can reject it.
SMALLEST_OUTPUT = '{"ch":"ticks","data":"AAAA"}'


def _write_flood_line(line_bytes: int) -> None:
    """Stream one `step_done` line of about `line_bytes` packed with tiny outputs.

    Written in chunks rather than built as one string: the kernel may close
    the pipe partway through, and a fixture that materialised the whole line
    first would put its own footprint next to the kernel's.
    """
    envelope = '{"op":"step_done","out":[]}\n'
    count = max((line_bytes - len(envelope)) // (len(SMALLEST_OUTPUT) + 1), 1)
    chunk = ",".join([SMALLEST_OUTPUT] * 1024)
    try:
        sys.stdout.write('{"op":"step_done","out":[')
        written = 0
        while written < count:
            batch = min(1024, count - written)
            sys.stdout.write(chunk[: batch * (len(SMALLEST_OUTPUT) + 1) - 1]
                             if batch < 1024 else chunk)
            written += batch
            if written < count:
                sys.stdout.write(",")
        sys.stdout.write(']}\n')
        sys.stdout.flush()
    except BrokenPipeError:
        # The kernel rejected the line and closed the pipe. That is the point
        # of this fixture, not a fixture failure.
        os._exit(0)


def _write_observer(path: str | None) -> None:
    if path is None:
        return
    Path(path).write_text(f"{os.getpid()}\n{Path.cwd()}\n")


def _long_response(op: str, *, newline: bool) -> None:
    response = json.dumps({"op": op, "padding": "x" * 4096})
    sys.stdout.write(response)
    if newline:
        sys.stdout.write("\n")
    sys.stdout.flush()


def main(mode: str, observer: str | None = None) -> None:
    _write_observer(observer)
    for line in sys.stdin:
        message = json.loads(line)
        if message["op"] == "init":
            if mode == "ready-long":
                _long_response("ready", newline=True)
            else:
                print(json.dumps({"op": "ready"}), flush=True)
        elif message["op"] == "step":
            if mode == "step-unterminated":
                _long_response("step_done", newline=False)
            elif mode == "many":
                print(
                    json.dumps(
                        {
                            "op": "step_done",
                            "out": [
                                {"ch": "ticks", "data": "not-base64"},
                                {"ch": "ticks", "data": "not-base64"},
                            ],
                        }
                    ),
                    flush=True,
                )
            elif mode == "flood":
                _write_flood_line(
                    int(os.environ["SIL_TEST_FLOOD_LINE_BYTES"])
                )
            elif mode == "inline":
                print(
                    json.dumps(
                        {
                            "op": "step_done",
                            "out": [{"ch": "ticks", "data": PAYLOAD}],
                        }
                    ),
                    flush=True,
                )
            else:
                print(json.dumps({"op": "step_done", "out": []}), flush=True)
        elif message["op"] == "shutdown":
            return


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
