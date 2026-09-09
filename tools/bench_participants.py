#!/usr/bin/env python3
"""Process participants for the large-Message routing baseline (issue #61).

Two shapes, selected by the first argument, so one file covers both directions
across the kernel↔process boundary:

    bench_participants.py source [burst]   publishes `burst` messages per step
    bench_participants.py sink             drains every input

Both derive their channel and payload layout from the init line, so the same
command works for any bench schema and either transport. `burst` is a command
argument rather than an environment read, so it stays inside the hashed
manifest like every other input to a run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from sil.participant import StepParticipant, run  # noqa: E402


def _zero_fields(spec: dict) -> dict:
    """A payload of the declared layout, filled once and reused every step.

    Only the leading sequence field changes per publication, so the cost the
    baseline measures is routing rather than a participant building megabytes.
    """
    fields = {}
    for field in spec["fields"]:
        count = field.get("count")
        if count is None:
            fields[field["name"]] = 0
        elif field["type"] == "u8":
            fields[field["name"]] = bytes(count)
        else:
            fields[field["name"]] = [0] * count
    return fields


class BenchSource(StepParticipant):
    def __init__(self, burst: int):
        self._burst = burst
        self._channel = ""
        self._fields: dict = {}
        self._seq_field = ""
        self._seq = 0

    def on_init(self, init: dict) -> None:
        self._channel = next(
            ch for ch, info in init["channels"].items()
            if info["direction"] == "out"
        )
        spec = init["schemas"][init["channels"][self._channel]["schema"]]
        self._fields = _zero_fields(spec)
        self._seq_field = spec["fields"][0]["name"]

    def on_step(self, t, dt, inputs):
        outputs = []
        for _ in range(self._burst):
            self._fields[self._seq_field] = self._seq
            self._seq += 1
            outputs.append((self._channel, dict(self._fields)))
        return outputs


class BenchSink(StepParticipant):
    def __init__(self):
        self._taken = 0

    def on_step(self, t, dt, inputs):
        self._taken += len(inputs)
        return None


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("source", "sink"):
        raise SystemExit("usage: bench_participants.py <source|sink> [burst]")
    if sys.argv[1] == "sink":
        run(BenchSink())
    else:
        run(BenchSource(int(sys.argv[2]) if len(sys.argv) > 2 else 1))


if __name__ == "__main__":
    main()
