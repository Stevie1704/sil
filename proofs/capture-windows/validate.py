#!/usr/bin/env python3
"""Check the named experiment's functional and evidence invariants.

No time, overhead or capture-volume threshold is a correctness gate.
Sampling misses and permission denials remain measured negative results.
"""
import argparse
import json
from pathlib import Path

from analyze import summarize


def validate(root):
    report = summarize(root)
    if report["availability"] == "unavailable":
        return report
    rows = report["runs"]
    for variant, count in (("native", 1240), ("process", 620)):
        group = [r for r in rows if r["variant"] == variant]
        ordinary = next(r for r in group if r["mode"] == "ordinary")
        failed = next(r for r in group if r["mode"] == "ordinary-failure")
        for row in group:
            mode, analysis = row["mode"], row["analysis"]
            if mode in ("unclosed", "exhaustion"):
                continue
            expected = failed if mode in ("failure", "ordinary-failure") else ordinary
            if row["recording_sha256"] != expected["recording_sha256"] or row["exit_code"] != expected["exit_code"]:
                raise ValueError(f"functional outcome changed: {row['directory']}")
            if mode not in ("ordinary", "ordinary-failure", "failure"):
                if analysis["completed_activations"] != count:
                    raise ValueError("incorrect completed Activation count")
            if mode in ("empty", "unreached"):
                if row["controls"] or row["samples"]["count"]:
                    raise ValueError("empty/unreached selection enabled CPU samples")
                if analysis["selection_state"] != mode:
                    raise ValueError("incorrect empty/unreached state")
            if mode == "window":
                if [c["command"] for c in row["controls"]] != ["enable", "disable"]:
                    raise ValueError("normal window did not enable then disable")
                if not all(c["ack_ns"] for c in row["controls"]):
                    raise ValueError("normal window control acknowledgment missing")
                if len(analysis["known_selected_work"]) != (2 if variant == "native" else 1):
                    raise ValueError("equal-time Task/interval selection mismatch")
                if not analysis["setup_metadata"]["exec_before_enable"] or not analysis["setup_metadata"]["workload_mmap_before_enable"]:
                    raise ValueError("setup exec/module metadata not retained before enable")
            if mode == "loss" and "notification_loss" not in analysis["reasons"]:
                raise ValueError("notification backpressure did not create explicit loss")
            if mode == "limit" and row["stop_reason"] != "capture_limit":
                raise ValueError("capture limit not exercised")
            if mode == "reserve" and row["stop_reason"] != "storage_reserve":
                raise ValueError("storage reserve not exercised")
            if mode == "failure" and not analysis["failed_activations"]:
                raise ValueError("failed work incorrectly reported completed")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    report = validate(args.evidence)
    (args.evidence / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")
