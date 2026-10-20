"""Offline decoder for the proof's bounded events, not a SiL report contract."""
from __future__ import annotations

import argparse
import json
import struct
from collections import Counter, defaultdict
from pathlib import Path

FUNCTIONS = {1: "check_object", 2: "confirmed", 3: "confirms", 4: "select_lead"}
EVENT = struct.Struct("<QIHH")
MAX_INPUT_BYTES = 16 << 20


def read(path: Path) -> bytes:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"proof input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    return path.read_bytes()


def analyze(events: Path, observations: Path) -> dict:
    raw = read(events)
    meta = json.loads(read(Path(str(events) + ".json")))
    rows = [json.loads(line) for line in read(observations).splitlines()]
    header, footer = rows[0], rows[-1]
    problems = []
    if (meta["clock"] != "host_monotonic" or
            header.get("clock") != "host_monotonic" or
            header.get("runner_pid") != meta["pid"] or
            header.get("sil_activation_observations") != 1):
        raise ValueError("unverified process or clock identity")
    before, libc, after = meta["clock_bracket_ns"]
    if not before <= libc <= after or meta["clock_errors"]:
        raise ValueError("host clock bracket failed")
    if (len(raw) % EVENT.size or len(raw) != meta["bytes_written"] or
            len(raw) != meta["records"] * EVENT.size):
        problems.append("short_event_file")
    if meta["dropped"]:
        problems.append("dropped_events")
    if (footer.get("kind") != "end" or footer.get("dropped", 0) or
            footer.get("truncated") or footer.get("write_failed") or
            footer.get("records") != len(rows) - 2):
        problems.append("incomplete_activations")
    activations = [{**a, "calls": [], "call_order": [], "counts": {}}
                   for a in rows if a.get("kind") == "activation"]
    stacks: dict[int, list[dict]] = defaultdict(list)
    calls = []
    last = 0
    for sequence, (ns, tid, function, edge) in enumerate(
            EVENT.iter_unpack(raw[:len(raw) // EVENT.size * EVENT.size])):
        if ns < last:
            problems.append("nonmonotonic_events")
        last = ns
        if function not in FUNCTIONS or edge not in (0, 1, 2):
            problems.append("unknown_event")
            continue
        stack = stacks[tid]
        if edge == 0:
            call = {"function": FUNCTIONS[function], "begin_ns": ns,
                    "end_ns": None, "elapsed_host_ns": None, "tid": tid,
                    "depth": len(stack), "sequence": sequence}
            stack.append(call)
            calls.append(call)
        elif not stack or stack[-1]["function"] != FUNCTIONS[function]:
            problems.append("unmatched_exit")
            stack.clear()
        else:
            call = stack.pop()
            if ns >= call["begin_ns"]:
                call.update(end_ns=ns, elapsed_host_ns=ns - call["begin_ns"],
                            exit_kind="tail" if edge == 2 else "return")
    if any(stacks.values()):
        problems.append("unclosed_calls")
    unattributed = []
    for call in calls:
        matches = [a for a in activations
                   if a["tid"] == call["tid"] == header["runner_tid"] and
                   a["task_kind"] == "native_task" and
                   a["outcome"] == "completed" and
                   int(a["begin_ns"]) <= call["begin_ns"] < int(a["end_ns"]) and
                   call["end_ns"] is not None and
                   call["end_ns"] < int(a["end_ns"])]
        if len(matches) == 1 and not problems:
            matches[0]["calls"].append(call)
        else:
            unattributed.append(call)
    for a in activations:
        a["call_order"] = [c["function"] for c in a["calls"]]
        a["counts"] = dict(Counter(a["call_order"]))
    return {"status": "partial" if problems else "complete",
            "problems": sorted(set(problems)), "clock": "host_monotonic",
            "metadata": meta, "events": len(raw) // EVENT.size,
            "activations": activations, "unattributed": unattributed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path)
    parser.add_argument("observations", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    args.out.write_text(json.dumps(analyze(args.events, args.observations),
                                    indent=2) + "\n")
