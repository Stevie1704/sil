"""Judge a replay Run against the live Run whose source it stands in for.

    python3 replay_equivalence.py <case> <live.mcap> <replay.mcap>

The exit code is the verdict: 0 when the replayed boundary carried the live
stimulus and every retained participant produced the same operations, in the
same order, at the same FMI event times; 1 when one of them did not.

**What is compared, and what is not.** Two Runs of two Manifests are two
Manifest hashes, so their Recordings differ in bytes by construction: the
Manifests are not the same document, the participants are not the same set,
and the replay Run carries a Publisher the live one does not. What has to
match is the Message streams the two Runs share:

- the **boundary** — `can.node1.Tx`, published by the removed node in the live
  Run and by the Replay participant in the replay Run. This is the stimulus,
  and comparing it says the replay carried every source operation, in order,
  with the event times the live Run recorded;
- the **retained streams** — the receiving node's transmissions and both of
  the bus simulation FMU's. Nothing in the replay Run produces these but the
  participants the replay did not replace, so they are what a replaced source
  has to reproduce.

Per Channel, four things are compared and each is named in the report: the
count of Messages, their order, the FMI event time each states, and the
payload bytes. The publication Slot is deliberately left out. It is the Slot
the publishing activation ran in, and in the replay Run the Replay participant
publishes the boundary at the Slot the Recording stored rather than at the
Slot an FMU produced it in; what a replaced source owes the receiver is the
operation and the instant it crossed the terminal at.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sil import schema
from sil.recording import read_records

sys.path.insert(0, str(Path(__file__).resolve().parent))

from can_operations import decode  # noqa: E402
from connected_manifest import BUFFER_SCHEMA, SCHEMAS, SOURCES  # noqa: E402
from replay_manifest import BOUNDARY, RETAINED  # noqa: E402


def streams(recording: Path) -> dict[str, list[dict]]:
    """Every Message of every observed Channel, in the Recording's own order."""
    message_type = schema.load(SCHEMAS)[BUFFER_SCHEMA]
    observed: dict[str, list[dict]] = {channel: [] for channel in SOURCES}
    for channel, _, data in read_records(recording):
        if channel not in observed:
            continue
        fields = message_type.unpack(data)
        payload = fields["data"][:fields["data_length"]]
        observed[channel].append({
            "time_ns": fields["data_event_time_ns"],
            "payload_hex": payload.hex(),
            "operations": [operation.name for operation in decode(payload)],
        })
    return observed


def report(channel: str, messages: list[dict]) -> None:
    for index, message in enumerate(messages):
        print(
            f"  {channel:<16} {index:>2}  event {message['time_ns']:>11} ns  "
            f"{len(message['payload_hex']) // 2:>3} B  "
            f"{', '.join(message['operations'])}"
        )


def differs(channel: str, live: list[dict], replay: list[dict]) -> bool:
    """Report the first Message that differs, and whether any did.

    The count is reported before the contents: a stream that is short by one
    operation and a stream whose first operation changed are different
    failures, and saying which one this is costs one line.
    """
    if len(live) != len(replay):
        print(f"  {channel}: live recorded {len(live)} Messages and the "
              f"replay Run {len(replay)}")
    for index in range(min(len(live), len(replay))):
        if live[index] == replay[index]:
            continue
        print(f"  {channel}: Message {index} differs")
        print(f"    live   {json.dumps(live[index], sort_keys=True)}")
        print(f"    replay {json.dumps(replay[index], sort_keys=True)}")
        return True
    return len(live) != len(replay)


def compare(name: str, live_recording: Path, replay_recording: Path) -> bool:
    live, replay = streams(live_recording), streams(replay_recording)
    print(f"--- case {name} ---")
    print(f"  boundary {BOUNDARY}, replayed into the terminal it fed")
    report(BOUNDARY, replay[BOUNDARY])
    for channel in RETAINED:
        report(channel, replay[channel])
    faulted = differs(BOUNDARY, live[BOUNDARY], replay[BOUNDARY])
    retained = [
        channel for channel in RETAINED
        if differs(channel, live[channel], replay[channel])
    ]
    if faulted or retained:
        print(f"{name}: the replay Run is not equivalent to the live Run")
        return False
    counts = ", ".join(
        f"{channel} {len(replay[channel])}"
        for channel in (BOUNDARY, *RETAINED)
    )
    print(f"{name}: {counts} — every Message, order and event time as the "
          f"live Run recorded them")
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit(
            "usage: replay_equivalence.py <case> <live.mcap> <replay.mcap>"
        )
    return 0 if compare(argv[0], Path(argv[1]), Path(argv[2])) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
