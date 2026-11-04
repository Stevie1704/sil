#!/usr/bin/env python3
"""Report capture evidence and optionally check the named experiment.

No time, overhead or capture-volume threshold is a correctness gate.
Sampling misses and permission denials remain measured negative results.
"""
import argparse
import errno
import json
from pathlib import Path

from analyze import summarize
from model import CASES, CaptureWindow


def validate_killed(row, expected_count):
    analysis = row["analysis"]
    if row["exit_code"] == 0 or row["run_exit_code"] is not None:
        raise ValueError("SIGKILL case invented a completed Run verdict")
    if "activation_observations_incomplete" not in analysis["reasons"]:
        raise ValueError("SIGKILL case lost its missing-footer state")
    if analysis["completed_activations"] >= expected_count:
        raise ValueError("SIGKILL case claimed all Activations completed")
    if analysis["requested_end_state"] != "not_reached_run_killed":
        raise ValueError("SIGKILL case claimed its requested end was reached")


def validate_storage_fault(row, baseline):
    analysis = row["analysis"]
    if not row["storage_fault_injected"] or row["storage_fault_errno"] != errno.ENOSPC:
        raise ValueError("storage exhaustion was not actually injected")
    if row["run_exit_code"] == 0:
        if row["recording_sha256"] != baseline["recording_sha256"]:
            raise ValueError("surviving Run changed its Recording under ENOSPC")
    elif row["run_exit_code"] is not None:
        if "run_failed" not in analysis["reasons"]:
            raise ValueError("storage failure hid a failed Run")
    elif "unclosed_run_work" not in analysis["reasons"]:
        raise ValueError("storage failure hid an unknown Run outcome")
    if any(row["exports"].values()):
        if "export_incomplete" not in analysis["reasons"]:
            raise ValueError("truncated storage capture presented as complete")
    elif row["stop_reason"] != "storage_reserve":
        raise ValueError("exhaustion case neither lost data nor stopped at reserve")


def validate_window(row):
    analysis = row["analysis"]
    selection = CaptureWindow(**row["requested"])
    if [c["command"] for c in row["controls"]] != ["enable", "disable"]:
        raise ValueError("normal window did not enable then disable")
    if not all(c["ack_ns"] is not None for c in row["controls"]):
        raise ValueError("normal window acknowledgment missing")
    if analysis["requested_end_state"] != "observed":
        raise ValueError("normal window did not observe its requested end")
    if analysis["requested_end_notice"]["virtual_ns"] < selection.to_ns:
        raise ValueError("window end triggered before the requested boundary")
    selected = analysis["known_selected_work"]
    if len(selected) != (2 if row["variant"] == "native" else 1):
        raise ValueError("equal-time Task selection mismatch")
    if any(i["index"] != 610 or not selection.contains(i["virtual_ns"], i["task"]) for i in selected):
        raise ValueError("Activation 611 entered the real selected region")
    excluded = analysis["first_excluded_work"]
    if excluded is None or excluded["index"] != 611 or excluded["virtual_ns"] != selection.to_ns:
        raise ValueError("real next-Activation edge was not retained")
    setup = analysis["setup_metadata"]
    if not setup["exec_before_enable"] or not setup["workload_mmap_before_enable"]:
        raise ValueError("setup exec/module metadata missing")


def validate_selection(row):
    mode, analysis = row["mode"], row["analysis"]
    if mode in ("empty", "unreached"):
        if row["controls"] or row["samples"]["count"] or analysis["selection_state"] != mode:
            raise ValueError("empty/unreached selection unexpectedly sampled CPU")
    if mode == "window":
        validate_window(row)
    if mode == "failure":
        if not analysis["failed_activations"]:
            raise ValueError("failed work reported completed")
        if analysis["requested_end_state"] != "not_reached_run_failed":
            raise ValueError("failure claimed its future Virtual-time end was reached")
        if analysis["selection_state"] != "terminated_before_requested_end":
            raise ValueError("failed selection hid early termination")


def validate_run(row, baseline, failure_baseline, expected_count):
    case, analysis = CASES[row["mode"]], row["analysis"]
    if case.required_reason and case.required_reason not in analysis["reasons"]:
        raise ValueError(f"required evidence state missing: {case.required_reason}")
    if case.outcome == "killed":
        validate_killed(row, expected_count)
    elif case.outcome == "storage_fault":
        validate_storage_fault(row, baseline)
    else:
        expected = failure_baseline if case.outcome == "failed" else baseline
        if row["recording_sha256"] != expected["recording_sha256"] or row["exit_code"] != expected["exit_code"]:
            raise ValueError(f"functional outcome changed: {row['directory']}")
        if case.runner != "ordinary" and case.outcome == "complete":
            if analysis["completed_activations"] != expected_count:
                raise ValueError("incorrect completed Activation count")
        validate_selection(row)


def validate(root):
    report = summarize(root)
    if report["availability"] == "unavailable":
        return report
    for variant, count in (("native", 1240), ("process", 620)):
        group = [row for row in report["runs"] if row["variant"] == variant]
        baseline = next(row for row in group if row["mode"] == "ordinary")
        failed = next(row for row in group if row["mode"] == "ordinary-failure")
        for row in group:
            validate_run(row, baseline, failed, count)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--report-only", action="store_true", help="report a partial fixture without checking the entire named experiment")
    args = parser.parse_args()
    report = summarize(args.evidence) if args.report_only else validate(args.evidence)
    (args.evidence / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")
