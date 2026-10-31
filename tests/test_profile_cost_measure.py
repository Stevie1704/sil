"""The tool that separates runner work from profile finalization (#329).

The tests read counts, states and arithmetic. No test reads a duration: a
retained value is evidence, not a gate.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from conftest import ROOT

TOOL = ROOT / "tools" / "profile_cost_measure.py"
sys.path.insert(0, str(ROOT / "tools"))

from profile_cost_measure import split_phases, verify_profile_hashes  # noqa: E402


def test_the_phases_add_up_to_the_end_to_end_time():
    phases = split_phases(start_ns=100, runner_end_ns=400, end_ns=1000,
                          profile_hash_ns=150)

    assert phases == {"end_to_end_ns": 900, "capture_and_run_ns": 300,
                      "post_run_ns": 600, "profile_hash_ns": 150,
                      "post_run_without_hash_ns": 450}


def test_a_runner_end_before_the_start_is_not_evidence():
    with pytest.raises(ValueError):
        split_phases(start_ns=100, runner_end_ns=99, end_ns=1000,
                     profile_hash_ns=0)


def test_a_false_profile_hash_is_not_evidence(tmp_path):
    artifact = tmp_path / "a"
    artifact.write_bytes(b"x")

    with pytest.raises(ValueError, match="does not match"):
        verify_profile_hashes(
            [{"path": "a", "bytes": 1, "sha256": "0" * 64}], tmp_path)


@pytest.fixture(scope="module")
def measured(build_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("evidence")
    done = subprocess.run(
        [sys.executable, str(TOOL), "--build-dir", str(build_dir),
         "--machine", "test", "--load-condition", "test", "--repetitions", "1",
         "--scale", "0.001", "--case", "cpu", "--case", "cpu-activations",
         "--label", "test", "--out-dir", str(out)],
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads((out / "profile-cost-test.json").read_text())


@pytest.mark.parametrize("case", ["cpu", "cpu-activations"])
def test_each_policy_hashes_each_profile_artifact_once(measured, case):
    for mode in ("full", "external"):
        run = measured["cases"][case][mode]["runs"][0]
        rows = run["profile_hashes"]
        assert len({row["path"] for row in rows}) == len(rows)
        assert {"run.mcap", "run.diagnostics.json"} <= {
            row["path"] for row in rows}


def test_the_runner_state_stays_apart_from_the_profile_hash(measured):
    cases = measured["cases"]["cpu"]
    full = cases["full"]["runs"][0]
    external = cases["external"]["runs"][0]

    assert full["runner"]["recording_state"] == "calculated"
    assert external["runner"]["recording_state"] == "omitted"
    assert full["runner"]["input_digests"]["entries"] > 0
    assert external["runner"]["input_digests"] == {
        "entries": 0, "distinct_paths": 0, "distinct_bytes": 0}
    # External mode still hashes the Recording as profile evidence.
    recording = [row for row in external["profile_hashes"]
                 if row["path"] == "run.mcap"]
    assert recording[0]["bytes"] == external["recording_bytes"] > 0


def test_the_policies_write_equal_recordings(measured):
    digests = {run["recording_sha256"]
               for case in measured["cases"].values()
               for policy in case.values() for run in policy["runs"]}
    assert len(digests) == 1
    assert all(len(digest) == hashlib.sha256().digest_size * 2
               for digest in digests)


def test_the_evidence_states_the_collector_and_its_limits(measured):
    assert measured["conditions"]["tools"] == "fake perf"
    assert "does not sample" in measured["conditions"]["tools_statement"]
    assert measured["conditions"]["runner_end"].startswith(
        "Modification time of the side-car")
