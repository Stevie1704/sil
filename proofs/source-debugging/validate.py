"""Validate saved selected-Activation evidence without GDB or workload binaries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_capture(dest: Path, form: str) -> None:
    stops = json.loads((dest / "stops.json").read_text())
    observations = [json.loads(line) for line in
                    (dest / "observations.jsonl").read_text().splitlines()]
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("form", choices=("native", "process", "fmu", "shim"))
    args = parser.parse_args()
    validate_capture(args.capture, args.form)
    print("2 selected Activations verified")
