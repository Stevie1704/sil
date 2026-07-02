"""Python side of the kernel's out-of-process step protocol.

A participant subclasses StepParticipant and hands an instance to run().
The kernel steps it over JSON lines on stdin/stdout; payloads are decoded
to and from field dicts using the schemas delivered in the init message.

The participant contract from DESIGN #6 applies: no wall-clock reads, no
free-running threads — all behavior must derive from (t, dt, inputs).
"""

from __future__ import annotations

import base64
import json
import sys
import traceback
from dataclasses import dataclass

from sil import schema


class ParticipantFailure(Exception):
    """Raised by a participant to abort the whole run."""


@dataclass(frozen=True)
class Input:
    channel: str
    publish_ns: int
    data: dict


class StepParticipant:
    def on_init(self, init: dict) -> None:
        pass

    def on_step(self, t: int, dt: int, inputs: list[Input]):
        """Advance from t to t+dt. Returns [(channel, fields_dict), ...]
        to publish at t, or None."""
        return None


def run(participant: StepParticipant) -> None:
    stdin = sys.stdin
    stdout = sys.stdout
    types_by_channel: dict[str, schema.MessageType] = {}

    def send(msg: dict) -> None:
        stdout.write(json.dumps(msg) + "\n")
        stdout.flush()

    for line in stdin:
        msg = json.loads(line)
        op = msg["op"]
        if op == "init":
            types = schema.load(msg["schemas"])
            types_by_channel = {
                ch: types[info["schema"]]
                for ch, info in msg["channels"].items()
            }
            participant.on_init(msg)
            send({"op": "ready"})
        elif op == "step":
            inputs = [
                Input(
                    i["ch"],
                    i["t"],
                    types_by_channel[i["ch"]].unpack(base64.b64decode(i["data"])),
                )
                for i in msg["in"]
            ]
            try:
                outputs = participant.on_step(msg["t"], msg["dt"], inputs) or []
            except Exception as e:  # noqa: BLE001 — any error must abort the run
                reason = "".join(traceback.format_exception_only(e)).strip()
                send({"op": "fail", "reason": reason})
                continue
            send({
                "op": "step_done",
                "out": [
                    {
                        "ch": ch,
                        "data": base64.b64encode(
                            types_by_channel[ch].pack(**fields)
                        ).decode(),
                    }
                    for ch, fields in outputs
                ],
            })
        elif op == "shutdown":
            return
