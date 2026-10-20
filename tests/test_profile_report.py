"""The `sil_profile_report` v1 contract (#315): its fixtures validate.

Every valid fixture is one case of docs/profile-report.md. Every invalid
case breaks exactly one rule of it: invalid/mutations.json names a valid
fixture and the members to change. The fixtures are hand-written data, so they
pin the contract independently of the code that writes a report.
"""

from __future__ import annotations

import copy
import json

import pytest

from conftest import ROOT
from sil.profile_report import (
    ProfileReportError, load_profile_report, parse_profile_report,
    validate_profile_report,
)

FIXTURES = ROOT / "tests" / "fixtures" / "profile"
VALID = sorted(FIXTURES.glob("*.json"))
MUTATIONS = json.loads((FIXTURES / "invalid" / "mutations.json").read_text())


def invalid_document(name):
    """The valid base fixture of `name` with its listed members changed."""
    mutation = MUTATIONS[name]
    document = copy.deepcopy(
        json.loads((FIXTURES / f"{mutation['base']}.json").read_text()))
    for change in mutation["changes"]:
        *parents, member = change["path"]
        target = document
        for key in parents:
            target = target[key]
        if change.get("delete"):
            del target[member]
        else:
            target[member] = change["value"]
    return document


def test_every_documented_case_has_a_fixture():
    names = {path.stem for path in VALID}
    assert {"cpu-complete", "cpu-zero-samples", "cpu-missing-symbols",
            "cpu-partial-lost", "cpu-lost-unknown-count", "cpu-run-failed",
            "cpu-not-started", "cpu-interrupted", "cpu-capture-absent",
            "cpu-runner-killed", "heap-reserved-runner",
            "heap-reserved-process", "heap-unsupported-scope",
            "activation-reserved", "activation-complete",
            "activation-partial-truncated", "heap-complete-runner",
            "heap-complete-process", "heap-partial-limit",
            "heap-failed-no-data", "heap-interrupted"} <= names


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixture_has_no_violation(path):
    assert validate_profile_report(json.loads(path.read_text())) == []
    assert load_profile_report(path)["sil_profile_report"] == 1


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_invalid_fixture_is_rejected(name, tmp_path):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(invalid_document(name)))

    with pytest.raises(ProfileReportError):
        load_profile_report(path)


EXPECTED_VIOLATION = {
    "unsupported-version": "unsupported sil_profile_report version 2",
    "unknown-capture-kind": "capture.kind: unknown kind 'gpu'",
    "unknown-correlation-kind": "activation_correlation.kind: unknown kind "
                                "'guess'",
    "null-without-reason": "capture.totals.lost_samples is null without a "
                           "reason",
    "process-samples-do-not-add-up": "capture.processes: samples add up to "
                                     "11, totals.samples is 10",
    "self-remainder-does-not-add-up": "capture.hotspots: self rows and "
                                      "self_remainder add up to 11 samples, "
                                      "totals.samples is 10",
    "cpu-complete-without-samples-file":
        "capture: status 'complete' without a samples file",
    "complete-with-reasons": "capture: status 'complete' with reasons",
    "partial-without-reasons": "capture: status 'partial' without a reason",
    "timestamp-not-a-string": "capture.time_origin_ns is not a decimal "
                              "string",
    "unknown-process-reference": "capture.threads[0].process: unknown "
                                 "process 'p9'",
    "succeeded-with-nonzero-exit": "run: status 'succeeded' with exit code 1",
    "unknown-debug-status": "capture.modules[3].debug.status: unknown value "
                            "'probably'",
    "artifact-outside-directory": "artifacts[0].path: not a relative path "
                                  "inside the profile directory",
    "missing-sampling": "missing member 'capture.sampling'",
    "inclusive-below-self": "capture.hotspots.inclusive[2]: inclusive "
                            "samples below self samples",
    "source-line-from-mismatched-debug": "capture.hotspots.self[5]: source "
                                         "lines from module 'm5' whose debug "
                                         "status is 'separate_mismatched'",
    "cpu-weight-called-duration": "capture.sampling.weight_unit: unknown "
                                  "value 'duration_ns'",
    "heap-analysis-invented": "missing member 'capture.analysis.totals'",
    "heap-self-rows-do-not-add-up":
        "capture.analysis.hotspots: self rows and self_remainder add up to "
        "9 allocations, totals.allocations is 8",
    "heap-frees-do-not-match-outstanding":
        "capture.analysis.totals: frees 4 is not allocations 8 minus "
        "outstanding_allocations 3",
    "heap-complete-without-allocations-file":
        "capture: status 'complete' without an allocations file",
    "heap-complete-without-analysis":
        "capture: status 'complete' without an analysis",
    "heap-unknown-reason": "capture.reasons: unknown reasons "
                           "['rss_too_high']",
    "heap-peak-below-outstanding":
        "capture.analysis.totals: peak_tracked_bytes 100 is below "
        "outstanding_bytes 65560",
    "heap-unknown-scope-kind": "capture.scope.kind: unknown kind "
                               "'native_participant'",
    "activation-classes-do-not-add-up":
        "activation_correlation.summary.samples: the classes add up to 11 "
        "samples, total is 10",
    "activation-unknown-unattributed-reason":
        "activation_correlation.summary.samples.unattributed.reasons[0]."
        "reason: unknown value 'probably_the_fmu'",
    "activation-task-windows-do-not-add-up":
        "activation_correlation.summary.tasks: window_associated samples "
        "add up to 2, samples.window_associated is 3",
    "activation-complete-without-summary":
        "activation_correlation: status 'complete' without a summary",
    "activation-unknown-process":
        "activation_correlation.summary.processes[0].process: unknown "
        "process 'p9'",
}


@pytest.mark.parametrize("name", sorted(EXPECTED_VIOLATION))
def test_invalid_fixture_names_its_violation(name):
    assert EXPECTED_VIOLATION[name] in validate_profile_report(
        invalid_document(name))


def test_every_invalid_fixture_has_an_expected_violation():
    assert set(MUTATIONS) == set(EXPECTED_VIOLATION)


def test_not_finite_json_is_rejected_by_the_parser():
    document = json.loads((FIXTURES / "cpu-complete.json").read_text())
    document["capture"]["sampling"]["frequency_hz"] = float("nan")
    text = json.dumps(document)

    with pytest.raises(ProfileReportError, match="NaN"):
        parse_profile_report(text)


def test_a_non_object_is_rejected():
    assert validate_profile_report([]) == [
        "not a sil_profile_report document"]
