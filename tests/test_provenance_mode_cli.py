"""`--provenance-mode` through `sil run`, `sil benchmark` and `sil profile` (#327).

Each command runs as its own process, as a user starts it. The policy reaches
the runner, and each report says which policy its measurement had.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from profile_sets import artifact_set, fixture
from sil.diagnostics_format import provenance_mode_text
from sil.diagnostics_report import validate_benchmark_report, validate_run_report
from sil.profile_report import validate_profile_report
from sil.profile_run import request_from_arguments
from test_profile_diff import diff
from test_profile_heap_run import (
    heap, heaptrack, marker_manifest, needs_linux,
)
from test_profile_heap_run import profile as heap_profile
from test_profile_heap_run import sil as heap_sil
from test_profile_render import render
from test_profile_run import exports, profile, sil as profile_sil, tools
from test_provenance_mode import replay_run
from test_run_diagnostics import failing_step_manifest, mixed_period_manifest
from test_run_diagnostics_cli import fixture as diagnostics_fixture
from test_run_diagnostics_cli import native_path, sil

__all__ = ["exports", "heaptrack", "native_path", "tools"]

INVALID_MODES = [
    (["--provenance-mode", "none"], "invalid choice"),
    (["--provenance-mode", "full", "--provenance-mode", "external"],
     "may be given only once"),
    (["--provenance-mode", "full", "--provenance-mode", "full"],
     "may be given only once"),
    (["--provenance-mode"], "expected one argument"),
]


def side_car(path: Path) -> dict:
    return json.loads(path.read_text())


# --- sil run ----------------------------------------------------------------


def test_ordinary_sil_run_forwards_the_policy(native_path, tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(["run", str(manifest), "-o", "run.mcap",
                "--provenance-mode", "external"], native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert side_car(tmp_path / "run.mcap.provenance.json")[
        "provenance_mode"] == "external"
    assert not (tmp_path / "run.mcap.diagnostics.json").exists()


@pytest.mark.parametrize("options, mode", [
    ([], "full"), (["--provenance-mode", "full"], "full"),
    (["--provenance-mode", "external"], "external")],
    ids=["default", "full", "external"])
def test_diagnostics_run_forwards_and_reports_the_policy(
        native_path, tmp_path, options, mode):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(["run", str(manifest), "--diagnostics", "-o", "run.mcap",
                "--html", "run.html", *options], native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    report = side_car(tmp_path / "run.mcap.diagnostics.json")
    assert validate_run_report(report) == []
    assert report["configuration"]["provenance_mode"] == mode
    assert side_car(tmp_path / "run.mcap.provenance.json")[
        "provenance_mode"] == mode
    assert f"provenance mode: {mode}" in proc.stdout
    assert "Provenance mode" in (tmp_path / "run.html").read_text()


@pytest.mark.parametrize("argv, message", INVALID_MODES)
def test_diagnostics_run_rejects_an_invalid_policy(native_path, tmp_path, argv,
                                                   message):
    proc = sil(["run", "manifest.json", "--diagnostics", *argv], native_path,
               tmp_path)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert list(tmp_path.iterdir()) == [native_path]


def test_diagnostics_custom_side_car_and_no_recording(native_path, tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(["run", str(manifest), "--diagnostics", "--no-recording",
                "--provenance", "custom.json",
                "--provenance-mode", "external"], native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    document = side_car(tmp_path / "custom.json")
    assert document["provenance_mode"] == "external"
    assert document["recording"]["digest_state"] == "not_requested"


def test_a_failed_diagnostics_run_keeps_its_exit_code_and_policy(native_path,
                                                                 tmp_path):
    manifest = failing_step_manifest(tmp_path)

    proc = sil(["run", str(manifest), "--diagnostics", "-o", "run.mcap",
                "--provenance-mode", "external"], native_path, tmp_path)

    assert proc.returncode == 1
    report = side_car(tmp_path / "run.mcap.diagnostics.json")
    assert report["configuration"]["provenance_mode"] == "external"
    document = side_car(tmp_path / "run.mcap.provenance.json")
    assert document["run_exit_code"] == 1
    assert document["recording"] == {"digest_state": "omitted", "sha256": None}


def test_a_diagnostics_report_before_the_option_stays_valid_and_reads_as_full():
    report = diagnostics_fixture("run-success.json")
    del report["configuration"]["provenance_mode"]
    assert validate_run_report(report) == []
    assert provenance_mode_text(report["configuration"]) == \
        "full (report predates the option)"


def test_a_diagnostics_report_with_an_unknown_policy_is_rejected():
    report = copy.deepcopy(diagnostics_fixture("run-success.json"))
    report["configuration"]["provenance_mode"] = "partial"
    assert validate_run_report(report)


# --- sil benchmark ----------------------------------------------------------


@pytest.mark.parametrize("options, mode", [
    ([], "full"), (["--provenance-mode", "external"], "external")],
    ids=["default", "external"])
def test_benchmark_forwards_and_reports_the_policy(native_path, tmp_path,
                                                   options, mode):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(["benchmark", str(manifest), "--repeats", "1", "--warmup", "1",
                "--out", "bench", "--html", "bench.html", *options],
               native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    summary = side_car(tmp_path / "bench" / "summary.json")
    assert validate_benchmark_report(summary) == []
    assert summary["policy"]["provenance_mode"] == mode
    assert f"provenance mode: {mode}" in proc.stdout
    assert "Provenance mode" in (tmp_path / "bench.html").read_text()
    for run in summary["runs"]:
        assert run["report"]["configuration"]["provenance_mode"] == mode
        assert side_car(tmp_path / "bench" / run["directory"]
                        / "run.mcap.provenance.json")["provenance_mode"] == mode


@pytest.mark.parametrize("argv, message", INVALID_MODES)
def test_benchmark_rejects_an_invalid_policy(native_path, tmp_path, argv,
                                             message):
    proc = sil(["benchmark", "manifest.json", "--out", "bench", *argv],
               native_path, tmp_path)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert not (tmp_path / "bench").exists()


def test_a_benchmark_report_before_the_option_stays_valid():
    summary = diagnostics_fixture("benchmark-complete.json")
    summary["policy"].pop("provenance_mode", None)
    assert validate_benchmark_report(summary) == []


# --- sil profile, cpu -------------------------------------------------------


@pytest.mark.parametrize("activations", [False, True],
                         ids=["plain", "activations"])
@pytest.mark.parametrize("options, mode", [
    ([], "full"), (["--provenance-mode", "full"], "full"),
    (["--provenance-mode", "external"], "external")],
    ids=["default", "full", "external"])
def test_cpu_capture_forwards_and_records_the_policy(
        tools, exports, tmp_path, options, mode, activations):
    manifest = mixed_period_manifest(tmp_path)
    extra = ["--activations"] if activations else []

    proc = profile_sil([str(manifest), "--out", "prof", "--collector", "cpu",
                        *extra, *options], tools, tmp_path, exports)

    out = tmp_path / "prof"
    report = profile(out)
    assert report["configuration"]["provenance_mode"] == mode
    assert report["run"]["status"] == "succeeded", proc.stderr
    assert side_car(out / "run.mcap.provenance.json")[
        "provenance_mode"] == mode
    assert side_car(out / "run.diagnostics.json")["configuration"][
        "provenance_mode"] == mode


def test_external_cpu_capture_still_hashes_every_profile_artifact(
        tools, exports, tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = profile_sil([str(manifest), "--out", "prof", "--collector", "cpu",
                        "--provenance-mode", "external"], tools, tmp_path,
                       exports)

    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "prof"
    report = profile(out)
    roles = {artifact["role"]: artifact for artifact in report["artifacts"]}
    assert "recording" in roles
    for artifact in report["artifacts"]:
        assert artifact["sha256"] == hashlib.sha256(
            (out / artifact["path"]).read_bytes()).hexdigest()
    # The profile tool's digest does not become a runner digest.
    assert side_car(out / "run.mcap.provenance.json")["recording"] == {
        "digest_state": "omitted", "sha256": None}


def test_external_profile_rejects_tampered_normalized_evidence(
        tools, exports, tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    proc = profile_sil([str(manifest), "--out", "prof", "--collector", "cpu",
                        "--provenance-mode", "external"], tools, tmp_path,
                       exports)
    assert proc.returncode == 0, proc.stderr
    samples = tmp_path / "prof" / "cpu" / "samples.json"
    samples.write_text(samples.read_text() + " ")

    proc = profile_sil(["render", "prof/profile.json"], tools, tmp_path)

    assert proc.returncode == 2
    assert "SHA-256" in proc.stderr


def test_cpu_capture_keeps_no_recording_and_custom_paths(tools, exports,
                                                         tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = profile_sil([str(manifest), "--out", "prof", "--collector", "cpu",
                        "--no-recording", "--provenance", "custom.json",
                        "--provenance-mode", "external"], tools, tmp_path,
                       exports)

    assert proc.returncode == 0, proc.stderr
    assert profile(tmp_path / "prof")["configuration"][
        "provenance_mode"] == "external"
    assert side_car(tmp_path / "custom.json")["recording"] == {
        "digest_state": "not_requested", "sha256": None}


def test_a_failed_cpu_capture_run_keeps_its_exit_code(tools, exports,
                                                      tmp_path):
    manifest = failing_step_manifest(tmp_path)

    proc = profile_sil([str(manifest), "--out", "prof", "--collector", "cpu",
                        "--provenance-mode", "external"], tools, tmp_path,
                       exports)

    assert proc.returncode == 1
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "run_failure"
    assert report["configuration"]["provenance_mode"] == "external"


@pytest.mark.parametrize("mode", ["full", "external"])
def test_cpu_capture_of_a_replay_mismatch_is_a_configuration_error(
        tools, exports, tmp_path, run_sil, mode):
    ref, source = replay_run(tmp_path, run_sil)
    source.write_bytes(source.read_bytes() + b"\0")

    proc = profile_sil([str(ref.path), "--out", "prof", "--collector", "cpu",
                        "--provenance-mode", mode], tools, tmp_path, exports)

    assert proc.returncode == 2
    assert profile(tmp_path / "prof")["run"]["status"] == "configuration_error"


@pytest.mark.parametrize("collector", [["--collector", "cpu"],
                                       ["--collector", "heap", "--runner"]],
                         ids=["cpu", "heap"])
@pytest.mark.parametrize("argv, message", INVALID_MODES)
def test_profile_rejects_an_invalid_policy(tools, tmp_path, collector, argv,
                                           message):
    proc = profile_sil(["manifest.json", "--out", "prof", *collector, *argv],
                       tools, tmp_path)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert not (tmp_path / "prof").exists()


# --- sil profile, heap ------------------------------------------------------


@pytest.mark.parametrize("scope", [["--runner"], ["--participant", "worker"]],
                         ids=["runner", "process"])
@pytest.mark.parametrize("options, mode", [
    ([], "full"), (["--provenance-mode", "external"], "external")],
    ids=["default", "external"])
def test_a_heap_request_carries_the_policy_to_the_runner(scope, options, mode):
    request = request_from_arguments(
        ["manifest.json", "--out", "prof", "--collector", "heap", *scope,
         *options])

    arguments = request.target.run_request(Path("report.json")).runner_arguments()

    assert arguments[arguments.index("--provenance-mode") + 1] == mode


@needs_linux
@pytest.mark.parametrize("options, mode", [
    ([], "full"), (["--provenance-mode", "external"], "external")],
    ids=["default", "external"])
@pytest.mark.parametrize("scoped", [False, True], ids=["runner", "process"])
def test_heap_capture_forwards_and_records_the_policy(
        tools, heaptrack, tmp_path, options, mode, scoped):
    manifest, _markers = marker_manifest(tmp_path)
    scope = ["--participant", "scoped"] if scoped else ["--runner"]

    proc = heap_sil([*heap(manifest, *scope), *options], tools, tmp_path,
                    heaptrack)

    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "prof"
    report = heap_profile(out)
    assert report["configuration"]["provenance_mode"] == mode
    assert side_car(out / "run.mcap.provenance.json")[
        "provenance_mode"] == mode


# --- offline presentation ---------------------------------------------------


def report_with(mode) -> dict:
    report = fixture("cpu-complete.json")
    report["configuration"]["provenance_mode"] = mode
    return report


@pytest.mark.parametrize("value, shown", [
    ("full", "provenance mode: full\n"),
    ("external", "provenance mode: external\n"),
    (None, "provenance mode: unknown\n"),
], ids=["full", "external", "unknown"])
def test_render_shows_the_policy(tmp_path, value, shown):
    path = artifact_set(tmp_path / "set", "cpu-complete.json",
                        report=report_with(value))

    proc = render([str(path), "--html", "page.html"], tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert shown in proc.stdout
    assert "Provenance mode" in (tmp_path / "page.html").read_text()


def test_render_says_that_a_legacy_set_is_treated_as_full(tmp_path):
    path = artifact_set(tmp_path / "set", "cpu-complete.json")

    proc = render([str(path)], tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "provenance mode: full (report predates the option)" in proc.stdout


def test_a_report_with_an_unknown_policy_value_is_rejected():
    assert validate_profile_report(report_with("partial"))


@pytest.mark.parametrize("base, new, warned", [
    ("full", "external", True),
    ("external", "external", False),
    (None, "external", True),
    (None, None, False),
], ids=["differ", "same", "unknown-differs", "both-unknown"])
def test_diff_flags_differing_policies(tmp_path, base, new, warned):
    first = artifact_set(tmp_path / "base", "cpu-complete.json",
                         report=report_with(base))
    second = artifact_set(tmp_path / "new", "cpu-complete.json",
                          report=report_with(new))

    proc = diff(tmp_path, first, second)

    assert proc.returncode == 0, proc.stderr
    assert "Provenance mode" in proc.stdout
    assert ("WARNING: identities differ: Provenance mode" in proc.stdout) \
        == warned


def test_diff_treats_a_legacy_set_as_full(tmp_path):
    legacy = artifact_set(tmp_path / "base", "cpu-complete.json")
    full = artifact_set(tmp_path / "full", "cpu-complete.json",
                        report=report_with("full"))
    external = artifact_set(tmp_path / "ext", "cpu-complete.json",
                            report=report_with("external"))

    assert "identities differ" not in diff(tmp_path, legacy, full).stdout
    assert "Provenance mode" in diff(tmp_path, legacy, external).stdout.split(
        "\n")[1]
