"""Deliberately send malformed arena output for protocol conformance tests."""

import json
import mmap
import struct
import sys


_ARENA_HEADER = struct.Struct("<QQ")


def main(mode: str) -> None:
    """Send one stale-sequence or over-capacity output, then await shutdown."""
    arena = None
    channel = None
    capacity = None
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message["op"] == "init":
                channel, info = next(
                    (name, value)
                    for name, value in message["channels"].items()
                    if value.get("transport") == "shm"
                )
                capacity = info["shm_capacity"]
                arena_file = open(info["shm_path"], "r+b")
                arena = mmap.mmap(
                    arena_file.fileno(), _ARENA_HEADER.size + capacity
                )
                print(json.dumps({"op": "ready"}), flush=True)
            elif message["op"] == "step":
                if mode == "stale":
                    seq = 1
                elif mode == "capacity":
                    seq = 1
                    _ARENA_HEADER.pack_into(arena, 0, seq, capacity + 1)
                else:
                    raise ValueError(f"unknown mode: {mode}")
                print(
                    json.dumps({
                        "op": "step_done",
                        "out": [{"ch": channel, "shm_seq": seq}],
                    }),
                    flush=True,
                )
            elif message["op"] == "shutdown":
                return
    finally:
        if arena is not None:
            arena.close()


if __name__ == "__main__":
    main(sys.argv[1])
