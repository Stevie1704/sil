"""Judge one connected Run's Recording against the expected exchange.

`clocked_exchange.py` does this for one node on its own. This does it for the
group: two nodes through the bus simulation FMU, four observed terminals, and
one expectation written from both FMUs' sources before any Run.

    python3 connected_exchange.py <case> <recording.mcap>

The exit code is the verdict: 0 when every operation, its payload, its order,
its FMI event time and the Slot it was published in matched the expectation,
1 when one did not.

Each Channel is compared on its own and in the Recording's own order, because
a Channel is one terminal's activations and nothing else may appear on it.

Three times are involved, and only the first is an FMU's:

- **event** — the FMI event time the Message states. For everything the bus
  transmits, that is an instant **between two Slots**: the bus states its
  transmission time as a countdown interval of (44 + dataLength) bits at the
  configured baud rate, which is 480 us for these frames.
- **published** — the Slot the importer was activated in, which is where the
  Recording timestamps the Message. The group's finer grid reaches the
  Recording as a stated event time, never as a timestamp.
- the delivery time, one Latency later, which is the subscriber's and appears
  in the observer's own lines rather than here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sil import schema
from sil.recording import read_records

sys.path.insert(0, str(Path(__file__).resolve().parent))

from can_operations import decode
from connected_manifest import BUFFER_SCHEMA, SCHEMAS, SOURCES

EXPECTED = Path(__file__).resolve().parent / "connected_expected.json"


def case(name: str) -> dict:
    for declared in json.loads(EXPECTED.read_text())["cases"]:
        if declared["name"] == name:
            return declared
    raise SystemExit(f"the expected exchange states no case {name!r}")


def observed(recording: Path) -> dict[str, list[dict]]:
    """Every Message the Run recorded, by source terminal, in stored order."""
    message_type = schema.load(SCHEMAS)[BUFFER_SCHEMA]
    events: dict[str, list[dict]] = {
        source: [] for source, _ in SOURCES.values()
    }
    for channel, log_time, data in read_records(recording):
        if channel not in SOURCES:
            continue
        source, _ = SOURCES[channel]
        events[source].append(as_event(source, log_time,
                                       message_type.unpack(data)))
    return events


def as_event(source: str, published_ns: int, fields: dict) -> dict:
    """One Message in the shape `connected_expected.json` states an event in."""
    payload = fields["data"][:fields["data_length"]]
    return {
        "time_ns": fields["data_event_time_ns"],
        "published_ns": published_ns,
        "source": source,
        "payload_hex": payload.hex(),
        "operations": [
            {"name": operation.name, "fields": operation.fields}
            for operation in decode(payload)
        ],
    }


def compare(name: str, recording: Path) -> bool:
    """Report the first Message that differs, and whether any did."""
    declared = case(name)
    events = observed(recording)
    print(f"--- case {name} ---")
    for source, recorded in events.items():
        for event in recorded:
            names = ", ".join(
                operation["name"] for operation in event["operations"]
            )
            print(
                f"  {source:<18} event {event['time_ns']:>11} ns  published "
                f"{event['published_ns']:>10} ns  "
                f"{len(event['payload_hex']) // 2:>3} B  {names}"
            )
    total = 0
    for source, recorded in events.items():
        expected = [
            event for event in declared["events"] if event["source"] == source
        ]
        for index, expectation in enumerate(expected):
            if index >= len(recorded):
                print(f"{name}: expected {source} event {index} at "
                      f"{expectation['time_ns']} ns, and the Recording ended")
                return False
            if recorded[index] != expectation:
                print(f"{name}: {source} event {index} differs")
                print(f"  expected {json.dumps(expectation, sort_keys=True)}")
                print(f"  observed {json.dumps(recorded[index], sort_keys=True)}")
                return False
        if len(recorded) > len(expected):
            extra = recorded[len(expected)]
            print(f"{name}: unexpected {source} event {len(expected)}: "
                  f"{json.dumps(extra, sort_keys=True)}")
            return False
        total += len(recorded)
    print(f"{name}: {total} events on {len(events)} terminals, all as expected")
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: connected_exchange.py <case> <recording.mcap>")
    return 0 if compare(argv[0], Path(argv[1])) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
