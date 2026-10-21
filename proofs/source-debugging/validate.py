"""Validate saved selected-Activation evidence without GDB or workload binaries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_capture(dest: Path, form: str) -> None:
    stops = json.loads((dest / "stops.json").read_text())
    observations = [json.loads(line) for line in
                    (dest / "observations.jsonl").read_text().splitlines()]
    if form == "bus-group":
        validate_group(stops, observations)
        return
    activations = [r for r in observations if r.get("kind") == "activation"
                   and r["participant"] == "hazard"]
    if len(stops) != 6:
        raise ValueError("expected entry/decision/return for two Activations")
    for index, t, sequence, previous, limited in (
            (4, 40_000_000, 5, "-2", "-2.5"),
            (5, 50_000_000, 6, "-2.5", "-3")):
        before, decision, after = [s for s in stops if s["hit"] == index]
        activation = next(a for a in activations if a["index"] == index)
        if activation["virtual_time_ns"] != t or any(
                s["virtual_time_ns"] != t for s in (before, decision, after)):
            raise ValueError("Activation index / Virtual time mismatch")
        if not all(int(activation["begin_ns"]) <= s["host_monotonic_ns"] < int(activation["end_ns"])
                   for s in (before, decision, after)):
            raise ValueError("debugger stop outside observed Activation")
        if before["values"]["previous_command"]["value"] != previous:
            raise ValueError("previous command differs from enumerated fixture")
        for name, expected in {"sequence": str(sequence), "sample_time_ns": str(t + 10_000_000),
                               "limited_result": limited, "selected": "12", "mode": "1",
                               "target": "-3"}.items():
            if after["values"][name]["value"] != expected:
                raise ValueError(f"{name} differs from enumerated fixture: {after['values'][name]}")
        if form != "native" and before["pid"] == observations[0]["runner_pid"]:
            raise ValueError("Process inferior incorrectly identified as runner")
        if form == "native" and before["pid"] != observations[0]["runner_pid"]:
            raise ValueError("Native inferior is not runner")
        if form == "native" and any(s["ptid"][1] != activation["tid"]
                                    for s in (before, decision, after)):
            raise ValueError("Native thread ownership differs from Activation")
        if form == "shim" and before["shim_virtual_time"]["value"] != str(t):
            raise ValueError("shim Virtual time differs from Activation")


def validate_group(stops: list[dict], observations: list[dict]) -> None:
    if len(stops) != 3 or len({s["instance"] for s in stops}) != 3:
        raise ValueError("expected three distinct FMU instances")
    if len({s["pid"] for s in stops}) != 1 or stops[0]["pid"] == observations[0]["runner_pid"]:
        raise ValueError("FMUs do not share one child process")
    spawn = next(r for r in observations if r.get("kind") == "spawn" and r["participant"] == "bus_group")
    activation = next(r for r in observations if r.get("kind") == "activation"
                      and r["participant"] == "bus_group" and r["index"] == 2)
    for stop in stops:
        if (stop["parent_pid"] != spawn["pid"] or stop["children"].strip() or
                not stop["all_threads_stopped"] or not stop["state_unchanged_while_stopped"]):
            raise ValueError("group stop does not establish shared process ownership")
        if stop["hit"] != 2 or stop["virtual_time_ns"] != 200_000_000 or activation["virtual_time_ns"] != 200_000_000:
            raise ValueError("group stop differs from selected Activation")
        if not int(activation["begin_ns"]) <= stop["host_monotonic_ns"] < int(activation["end_ns"]):
            raise ValueError("group stop outside observed Activation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("form", choices=("native", "process", "fmu", "shim", "bus-group"))
    args = parser.parse_args()
    validate_capture(args.capture, args.form)
    print("3 grouped FMU instances verified" if args.form == "bus-group" else "2 selected Activations verified")
