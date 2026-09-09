#!/usr/bin/env python3
"""Process participants for the large-Message routing baseline (issue #61).

Two shapes, selected by the first argument, so one file covers both directions
across the kernel↔process boundary:

    bench_participants.py publisher [burst]   publishes `burst` Messages per step
    bench_participants.py subscriber          subscribes to one Channel

Both derive their channel and payload layout from the init line, so the same
command works for any bench schema and either transport. `burst` is a command
argument rather than an environment read, so it stays inside the hashed
manifest like every other input to a run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python" / "src"))

from sil.participant import StepParticipant, run  # noqa: E402


def _payload_template(spec: dict) -> dict:
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


class BenchPublisher(StepParticipant):
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
        self._fields = _payload_template(spec)
        self._seq_field = spec["fields"][0]["name"]

    def on_step(self, t, dt, inputs):
        outputs = []
        for _ in range(self._burst):
            self._fields[self._seq_field] = self._seq
            self._seq += 1
            outputs.append((self._channel, dict(self._fields)))
        return outputs


class BenchSubscriber(StepParticipant):
    """Subscribes to one Channel and publishes nothing.

    Its body is empty on purpose: the cost this side of the boundary
    contributes is the Transport decode, which the step loop has already done
    by the time on_step is reached."""


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("publisher", "subscriber"):
        raise SystemExit(
            "usage: bench_participants.py <publisher|subscriber> [burst]")
    if sys.argv[1] == "subscriber":
        run(BenchSubscriber())
    else:
        run(BenchPublisher(int(sys.argv[2]) if len(sys.argv) > 2 else 1))


if __name__ == "__main__":
    main()
