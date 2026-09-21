"""Judge one Run's Recording against the acceptance fixture's expectation.

`reference_exchange.py` drives the node through an independent importer and
compares what it observed with `expected.json`. This does the same job one
step further out: the node is driven by SiL, and what is compared is the
Recording the Run produced.

    python3 clocked_exchange.py <case> <recording.mcap>

The exit code is the verdict: 0 when every operation, its payload, its order
and its FMI event time matched the expectation, 1 when one did not.

Three times are printed for every Message, and only the first is the FMU's:

- **event** — the FMI event time the Message states, which is the
  communication point the activation was observed at;
- **published** — the Slot the importer was activated in, which is where the
  Recording timestamps the Message;
- the delivery time, one Latency later, which is the subscriber's and appears
  in the observer's own lines rather than here.

A 100 ms grid observes the node's 300 ms transmit at 300 ms and publishes it
in the Slot at 200 ms, because the Step from 200 ms to 300 ms is the one it
became visible in. That difference is the point of the two grids: nothing is
quantised to the Slot, and no Latency is folded into the event time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sil import schema
from sil.recording import read_records

sys.path.insert(0, str(Path(__file__).resolve().parent))

from can_operations import decode
from clocked_manifest import BUFFER_SCHEMA, CAN_CHANNEL, SCHEMAS

EXPECTED = Path(__file__).resolve().parent / "expected.json"


def case(name: str) -> dict:
    for declared in json.loads(EXPECTED.read_text())["cases"]:
        if declared["name"] == name:
            return declared
    raise SystemExit(f"the expected exchange states no case {name!r}")


def observed(recording: Path) -> list[tuple[int, dict]]:
    """Every Message the Run recorded on the CAN Channel, in stored order."""
    message_type = schema.load(SCHEMAS)[BUFFER_SCHEMA]
    return [
        (log_time, message_type.unpack(data))
        for topic, log_time, data in read_records(recording)
        if topic == CAN_CHANNEL
    ]


def as_event(fields: dict) -> dict:
    """One Message in the shape `expected.json` states an event in."""
    payload = fields["data"][:fields["data_length"]]
    return {
        "time_ns": fields["data_event_time_ns"],
        "payload_hex": payload.hex(),
        "operations": [
            {"name": operation.name, "fields": operation.fields}
            for operation in decode(payload)
        ],
    }


def expected_events(declared: dict) -> list[dict]:
    """The expected events, less the one field a Recording cannot carry.

    `next_event_time_s` is what `fmi3UpdateDiscreteStates` declared when the
    event ended. No Channel carries it, so this comparison does not observe
    it and does not pretend to: it requires every expectation to declare
    none, and an expectation that declared one would stop the comparison
    rather than be matched against a null this script wrote itself.
    """
    events = []
    for event in declared["events"]:
        if event["next_event_time_s"] is not None:
            raise SystemExit(
                f"the event at {event['time_ns']} ns expects "
                f"next_event_time_s {event['next_event_time_s']}, which no "
                f"Channel carries; this comparison cannot judge it"
            )
        events.append({
            key: value for key, value in event.items()
            if key != "next_event_time_s"
        })
    return events


def compare(name: str, recording: Path) -> bool:
    """Report the first Message that differs, and whether any did."""
    expected = expected_events(case(name))
    messages = observed(recording)
    print(f"--- case {name} ---")
    for published_ns, fields in messages:
        event = as_event(fields)
        names = ", ".join(
            operation["name"] for operation in event["operations"]
        )
        print(
            f"  event {event['time_ns']:>10} ns  published "
            f"{published_ns:>10} ns  {len(event['payload_hex']) // 2:>3} B  "
            f"{names}"
        )
    for index, expectation in enumerate(expected):
        if index >= len(messages):
            print(f"{name}: expected event {index} at "
                  f"{expectation['time_ns']} ns, and the Recording ended")
            return False
        actual = as_event(messages[index][1])
        if actual != expectation:
            print(f"{name}: event {index} differs")
            print(f"  expected {json.dumps(expectation, sort_keys=True)}")
            print(f"  observed {json.dumps(actual, sort_keys=True)}")
            return False
    if len(messages) > len(expected):
        extra = as_event(messages[len(expected)][1])
        print(f"{name}: unexpected event {len(expected)}: "
              f"{json.dumps(extra, sort_keys=True)}")
        return False
    print(f"{name}: {len(messages)} events, all as expected")
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: clocked_exchange.py <case> <recording.mcap>")
    return 0 if compare(argv[0], Path(argv[1])) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
