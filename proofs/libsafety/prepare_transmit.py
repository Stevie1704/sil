"""Export the transmit candidates and the Native form's reference (#232).

Runs in the public-workload tool image (issue #178) with no network, after
`prepare_frames.py`, in a process of its own: the library keeps its state in
C globals, so one process is one instance.

The pinned segment has no `sendcan`. The transmit candidates are therefore
derived from recorded frames, not generated: on this Toyota the stock camera
sends STEERING_LTA, STEERING_LKA, ACC_CONTROL and LKAS_HUD on bus 2, and
these are the messages openpilot sends in their place on bus 0. Every such
recorded camera frame becomes one transmit candidate on bus 0, with its
recorded payload. The candidates of one `can` event form one upstream
`sendcan` event at the same `logMonoTime`, after the `can` event.

The reference driver is upstream's replay policy (`replay_drive`), event by
event, through upstream's CFFI declarations and the pinned library:

- `can` event: `set_timer`, `safety_tick` when warm, then per received frame
  `safety_fwd_hook` and `safety_rx_hook`;
- `sendcan` event: `set_timer`, `safety_tick` when warm, then
  `safety_tx_hook` per candidate;
- after both, the observed state and the event's transmit verdicts.

Two cross-checks tie it to upstream and to #178. Upstream `replay_drive`
itself replays the same `can` and `sendcan` events and must count the same
received, invalid, transmitted and blocked frames. The receive-side state of
every event must equal the pinned #178 reference, so the candidates do not
change what #193 compares.

It writes into <out-directory>:

- `transmit.csv`: one row per candidate, the columns of `frames.csv`;
- `native-reference.json`: the trace, one state per `can` event;
- `transmit.json`: the digests, counts, verdicts and both cross-checks.

Usage: prepare_transmit.py <bundle> <opendbc> <public-workloads> <out-directory>
"""
import csv
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from prepare_frames import RECORDING, sha256
from workload import (
    BOOL_STATE,
    CAMERA_BUS,
    FLOAT_STATE,
    FRAME_COLUMNS,
    TRANSMIT_ADDRESSES,
    TRANSMIT_BUS,
    TRANSMIT_ECHO_SOURCE,
)

LIBRARY = Path("libsafety/libsafety.so")
REFERENCE = Path("references/libsafety-states.json")
WARM_UP_NS = 1_000_000_000
STATE = (*BOOL_STATE, *FLOAT_STATE)
# The receive-side fields the #178 reference records.
RECEIVE_FIELDS = ("t_ns", "accepted", "rejected", *STATE)


def transmit_candidates(frames):
    """The recorded camera frames openpilot replaces, readdressed to bus 0."""
    return [(address, TRANSMIT_BUS, data) for address, src, data in frames
            if src == CAMERA_BUS and address in TRANSMIT_ADDRESSES]


def timer_us(log_mono_ns):
    return (log_mono_ns // 1000) % 0xFFFFFFFF


def replay(lib, packet, contract, events):
    """One state per `can` event, with the verdicts of its `sendcan` event."""
    if lib.set_safety_hooks(contract["mode"], contract["param"]) != 0:
        raise RuntimeError(f"safety mode {contract['mode']} param "
                           f"{contract['param']} rejected")
    lib.set_alternative_experience(contract["alternative_experience"])
    first, last = events[0][0], events[-1][0]

    def start_event(t):
        lib.set_timer(timer_us(t))
        if t - first > WARM_UP_NS and last - t > WARM_UP_NS:
            lib.safety_tick()

    trace, verdicts = [], Counter()
    for t, frames in events:
        start_event(t)
        accepted = refused = 0
        for address, source, data in frames:
            if source >= TRANSMIT_ECHO_SOURCE:
                continue
            lib.safety_fwd_hook(source, address)
            if lib.safety_rx_hook(packet(address, source % 4, data)):
                accepted += 1
            else:
                refused += 1
        candidates = transmit_candidates(frames)
        sent = blocked = 0
        if candidates:
            start_event(t)
        for address, bus, data in candidates:
            allowed = bool(lib.safety_tx_hook(packet(address, bus, data)))
            sent += allowed
            blocked += not allowed
            verdicts[(f"{address:#x}", allowed)] += 1
        state = {name: getattr(lib, f"get_{name}")() for name in STATE}
        trace.append({"t_ns": t, "accepted": accepted, "rejected": refused,
                      "tx_accepted": sent, "tx_rejected": blocked,
                      **{k: v if isinstance(v, float) else bool(v)
                         for k, v in state.items()}})
    by_address = {}
    for (address, allowed), n in sorted(verdicts.items()):
        by_address.setdefault(address, {"accepted": 0, "rejected": 0})[
            "accepted" if allowed else "rejected"] = n
    return trace, by_address


def write_candidates(events, path: Path) -> int:
    rows = 0
    with path.open("w", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(FRAME_COLUMNS)
        for log_mono_ns, frames in events:
            for address, bus, data in transmit_candidates(frames):
                writer.writerow([log_mono_ns, address, bus, len(data),
                                 *data.ljust(8, b"\x00")])
                rows += 1
    return rows


# Upstream replay_drive reads `which()`, `logMonoTime` and the frames of
# `can` or `sendcan`. The events are given in that shape, `can` first.
UPSTREAM = """
import json, sys
from types import SimpleNamespace
sys.path[:0] = [sys.argv[2], sys.argv[3]]
from opendbc.safety.tests.libsafety import libsafety_py
libsafety_py.load(sys.argv[1])
from opendbc.safety.tests.safety_replay.replay_drive import replay_drive
from can_recording import read_events
from prepare_transmit import transmit_candidates

class Event:
    def __init__(self, kind, t, frames):
        self._kind, self.logMonoTime = kind, t
        setattr(self, kind, [SimpleNamespace(address=a, src=s, dat=d) for a, s, d in frames])
    def which(self):
        return self._kind

contract, events = read_events(sys.argv[4])
msgs = []
for t, frames in events:
    msgs.append(Event("can", t, frames))
    candidates = transmit_candidates(frames)
    if candidates:
        msgs.append(Event("sendcan", t, candidates))
print("passed:", replay_drive(msgs, contract["mode"], contract["param"],
                              contract["alternative_experience"]))
"""


def upstream_replay(library: Path, opendbc: str, public_workloads: str,
                    recording: Path) -> dict:
    here = str(Path(__file__).resolve().parent)
    out = subprocess.run(
        [sys.executable, "-c", UPSTREAM, str(library), opendbc,
         public_workloads, str(recording)],
        check=True, capture_output=True, text=True, cwd=here,
        env={"PYTHONPATH": here}).stdout

    def field(label):
        found = re.search(rf"^{label}: (.*)$", out, re.MULTILINE)
        if found is None:
            raise RuntimeError(f"upstream replay printed no {label!r}:\n{out}")
        return found.group(1)
    return {"received": int(field("total rx msgs")),
            "invalid": int(field("invalid rx msgs")),
            "transmitted": int(field("total openpilot msgs")),
            "blocked": int(field("blocked msgs")),
            "blocked_with_controls_allowed": int(field("blocked with controls allowed")),
            "blocked_addresses": field("blocked addrs")}


def receive_divergence(trace, pinned):
    """The first event whose receive-side state differs from #178's, or None."""
    if len(trace) != len(pinned):
        return {"field": "coverage", "expected": len(pinned), "actual": len(trace)}
    for index, (actual, expected) in enumerate(zip(trace, pinned)):
        for field in RECEIVE_FIELDS:
            if actual[field] != expected[field]:
                return {"index": index, "field": field,
                        "expected": expected[field], "actual": actual[field]}
    return None


def main(bundle, opendbc, public_workloads, out):
    sys.path[:0] = [opendbc, public_workloads]
    from can_recording import read_events
    from opendbc.safety.tests.libsafety import libsafety_py

    bundle, out = Path(bundle), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    contract, events = read_events(bundle / RECORDING)
    candidates = write_candidates(events, out / "transmit.csv")
    libsafety_py.load(str(bundle / LIBRARY))
    trace, by_address = replay(libsafety_py.libsafety, libsafety_py.make_CANPacket,
                               contract, events)
    (out / "native-reference.json").write_text(
        json.dumps({"contract": contract, "trace": trace}, allow_nan=False) + "\n")

    upstream = upstream_replay(bundle / LIBRARY, opendbc, public_workloads,
                               bundle / RECORDING)
    tx_accepted = sum(state["tx_accepted"] for state in trace)
    tx_rejected = sum(state["tx_rejected"] for state in trace)
    own = {"received": sum(s["accepted"] + s["rejected"] for s in trace),
           "invalid": sum(s["rejected"] for s in trace),
           "transmitted": tx_accepted + tx_rejected, "blocked": tx_rejected}
    divergence = receive_divergence(
        trace, json.loads((bundle / REFERENCE).read_text())["trace"])
    identity = {
        "recording": {"path": str(RECORDING), "sha256": sha256(bundle / RECORDING)},
        "library": {"path": str(LIBRARY), "sha256": sha256(bundle / LIBRARY)},
        "reference": {"path": str(REFERENCE), "sha256": sha256(bundle / REFERENCE)},
        "exporter_sha256": sha256(Path(__file__)),
        "transmit_csv_sha256": sha256(out / "transmit.csv"),
        "native_reference_sha256": sha256(out / "native-reference.json"),
        "candidates": {
            "kind": "recorded stock-camera frames (bus 2) readdressed to bus 0; "
                    "not openpilot sendcan",
            "addresses": [f"{a:#x}" for a in TRANSMIT_ADDRESSES],
            "frames": candidates,
            "events": sum(1 for _, frames in events if transmit_candidates(frames)),
            "largest_burst": max(len(transmit_candidates(f)) for _, f in events),
            "by_address": by_address,
        },
        "upstream_replay_drive": upstream,
        "reference_totals": own,
        "agrees_with_upstream": all(upstream[k] == own[k] for k in own),
        "receive_state_divergence_from_178": divergence,
    }
    (out / "transmit.json").write_text(json.dumps(identity, indent=2) + "\n")
    if not identity["agrees_with_upstream"] or divergence is not None:
        raise SystemExit(f"the transmit reference does not agree: upstream "
                         f"{upstream}, own {own}, receive divergence {divergence}")
    if not tx_accepted or not tx_rejected:
        raise SystemExit(f"the candidates must be both accepted and rejected: "
                         f"{by_address}")


if __name__ == "__main__":
    main(*sys.argv[1:])
