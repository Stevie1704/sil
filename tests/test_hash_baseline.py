"""The hashing baseline tool: attribution, evidence checks and runner selection.

No test asserts a duration: timings on a loaded machine are not a correctness
gate. They assert bytes, operation counts, digests and the shape of the evidence.
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest
from conftest import BUILD_DIR, ROOT

_spec = importlib.util.spec_from_file_location(
    "hash_baseline", ROOT / "tools" / "hash_baseline.py")
hash_baseline = importlib.util.module_from_spec(_spec)
sys.modules["hash_baseline"] = hash_baseline
_spec.loader.exec_module(hash_baseline)


def _span(offset, bytes_read, size, start=0, end=10):
    return {"offset": offset, "bytes": bytes_read, "size": size,
            "start_ns": start, "end_ns": end}


def _trace(*files):
    return {"process_start_ns": 0, "process_end_ns": 100, "dropped_files": 0,
            "files": [{"path": path, "opens": len(spans), "reads": len(spans),
                       "bytes": sum(s["bytes"] for s in spans),
                       "dropped_spans": 0, "spans": list(spans)}
                      for path, spans in files]}


ROLES = {"/m.json": "manifest", "/lib.silp": "provenance_inputs",
         "/replay.mcap": "replay_verification", "/out.mcap": "output"}


def test_complete_read_passes_count_as_hash_operations_per_role():
    work = hash_baseline.attribute_hash_work(
        _trace(("/m.json", [_span(0, 10, 10)]),
               ("/lib.silp", [_span(0, 100, 100, 5, 25),
                              _span(0, 100, 100, 30, 40)]),
               ("/out.mcap", [_span(0, 50, 50, 60, 90)])),
        ROLES)
    assert work["manifest"] == {"operations": 1, "bytes": 10, "time_ns": 10}
    assert work["provenance_inputs"] == {
        "operations": 2, "bytes": 200, "time_ns": 30}
    assert work["output"] == {"operations": 1, "bytes": 50, "time_ns": 30}
    assert work["replay_verification"] == {
        "operations": 0, "bytes": 0, "time_ns": 0}


def test_partial_reads_and_foreign_files_are_not_hash_work():
    work = hash_baseline.attribute_hash_work(
        _trace(("/lib.silp", [_span(0, 40, 100), _span(40, 10, 100)]),
               ("/etc/hosts", [_span(0, 7, 7)])),
        ROLES)
    assert work["provenance_inputs"]["operations"] == 0
    assert work["other_reads"]["bytes"] == 57


def test_only_the_first_complete_pass_of_a_replay_file_is_verification():
    work = hash_baseline.attribute_hash_work(
        _trace(("/replay.mcap", [_span(0, 80, 80, 0, 8), _span(70, 10, 80),
                                 _span(0, 80, 80, 20, 24)])),
        ROLES)
    assert work["replay_verification"] == {
        "operations": 1, "bytes": 80, "time_ns": 8}
    assert work["other_reads"]["bytes"] == 90


def test_a_full_trace_table_stops_the_attribution():
    trace = _trace(("/lib.silp", [_span(0, 100, 100)]))
    trace["files"][0]["dropped_spans"] = 1
    with pytest.raises(hash_baseline.EvidenceError, match="dropped"):
        hash_baseline.attribute_hash_work(trace, ROLES)


def test_a_claimed_digest_with_no_read_pass_is_named():
    side_car = {"artifacts": {"library": {"path": "/lib.silp", "sha256": "ab"},
                              "seen": {"path": "/seen", "sha256": "cd"}}}
    trace = _trace(("/seen", [_span(0, 5, 5)]),
                   ("/lib.silp", [_span(0, 2, 5)]))
    assert hash_baseline.inputs_without_observed_hash(side_car, trace) == [
        "/lib.silp"]


def test_side_car_digest_disagreeing_with_an_independent_hash_is_rejected(
        tmp_path):
    artifact = tmp_path / "lib.silp"
    artifact.write_bytes(b"abc")
    # The published SHA-256 test vector for "abc" (FIPS 180-4).
    good = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    side_car = {"artifacts": {"runner": {"path": str(artifact), "sha256": good},
                              "clock_shim": None},
                "recording": {"sha256": good}}
    hash_baseline.verify_references(side_car, artifact, {})
    side_car["artifacts"]["runner"]["sha256"] = "0" * 64
    with pytest.raises(hash_baseline.EvidenceError, match="lib.silp"):
        hash_baseline.verify_references(side_car, artifact, {})


def test_an_omitted_digest_state_is_not_a_reference_failure(tmp_path):
    artifact = tmp_path / "lib.silp"
    artifact.write_bytes(b"abc")
    side_car = {"artifacts": {"runner": {"path": str(artifact),
                                         "sha256": None,
                                         "digest_state": "omitted"}},
                "recording": {"digest_state": "omitted"}}
    checked = hash_baseline.verify_references(side_car, artifact, {})
    assert checked == {"inputs": 0, "recording_digest_present": False}


@pytest.fixture(scope="module")
def capture(tmp_path_factory):
    """One tiny capture of every workload, on the checkout's own build."""
    out = tmp_path_factory.mktemp("evidence")
    code = hash_baseline.main([
        "--build-dir", str(BUILD_DIR), "--machine", "test machine",
        "--load-condition", "pytest load", "--repetitions", "2",
        "--scale", "0.002", "--out-dir", str(out), "--label", "t"])
    assert code == 0
    return (json.loads((out / "hash-baseline-t.json").read_text()),
            (out / "hash-baseline-t-results.md").read_text())


def test_capture_records_conditions_needed_to_compare_it(capture):
    document, _ = capture
    assert document["host"]["machine"] == "test machine"
    assert document["conditions"]["load"] == "pytest load"
    assert document["conditions"]["repetitions"] == 2
    assert "cannot control" in document["conditions"]["cold_cache"]
    assert document["build"]["cmake_build_type"] == "Release"
    assert document["build"]["source_revision"]
    assert set(document["workloads"]) == {
        "native-shared-library", "native-distinct-artifacts",
        "native-recording-heavy", "process-shared-inputs",
        "replay-verification"}


def test_every_workload_has_distributions_and_equal_recordings(capture):
    document, _ = capture
    for workload in document["workloads"].values():
        summary = workload["summary"]
        assert summary["recordings_identical"] is True
        assert summary["end_to_end_ns"]["ordinary"]["count"] == 2
        assert summary["end_to_end_ns"]["attributed"]["count"] == 2
        for phase in ("preparation_ns", "execution_recording_ns",
                      "finalization_ns", "reported_run_wall_ns"):
            assert summary["phases"][phase]["count"] == 2
        assert workload["artifacts"]
        assert workload["recording_bytes"] > 0


def _input_bytes(workload, kind):
    return sum(a["bytes"] for a in workload["artifacts"] if a["kind"] == kind)


def _attributed(workload):
    return [r for r in workload["runs"] if r["series"] == "attributed"]


def test_shared_library_is_hashed_at_most_once_per_participant(capture):
    workload = capture[0]["workloads"]["native-shared-library"]
    library = _input_bytes(workload, "library")
    for run in _attributed(workload):
        hashed = run["hash_work"]["provenance_inputs"]
        # Runner plus library, at least once and at most once per Participant.
        assert 2 <= hashed["operations"] <= 1 + workload["participants"]
        assert library < hashed["bytes"]
        assert hashed["bytes"] <= (run["runner_bytes"]
                                   + library * workload["participants"])
        assert run["hash_work"]["manifest"]["operations"] == 1


def test_distinct_artifacts_are_each_hashed(capture):
    workload = capture[0]["workloads"]["native-distinct-artifacts"]
    hashed = _attributed(workload)[0]["hash_work"]["provenance_inputs"]
    assert hashed["bytes"] >= _input_bytes(workload, "library")
    assert hashed["operations"] == 1 + workload["participants"]


def test_process_workload_sees_the_shared_file_argument(capture):
    workload = capture[0]["workloads"]["process-shared-inputs"]
    hashed = _attributed(workload)[0]["hash_work"]["provenance_inputs"]
    assert hashed["bytes"] >= _input_bytes(workload, "file_argument")


def test_replay_verification_is_separate_from_provenance(capture):
    workload = capture[0]["workloads"]["replay-verification"]
    verified = _attributed(workload)[0]["hash_work"]["replay_verification"]
    assert verified["operations"] == 1
    assert verified["bytes"] == _input_bytes(workload, "replay_recording")


def test_output_hash_is_the_recording_or_is_stated_as_not_a_reread(capture):
    workload = capture[0]["workloads"]["native-recording-heavy"]
    for run in _attributed(workload):
        output = run["hash_work"]["output"]
        assert (output["bytes"] == workload["recording_bytes"]
                or run["output_digest_without_reread"] is True)


def test_every_claimed_input_digest_has_an_observed_hash_pass(capture):
    for workload in capture[0]["workloads"].values():
        for run in _attributed(workload):
            assert run["inputs_without_observed_hash"] == []


def test_results_table_names_the_timing_boundary_and_each_workload(capture):
    _, table = capture
    assert "pytest load" in table
    assert "excludes" in table
    for name in capture[0]["workloads"]:
        assert name in table


def test_a_workload_selection_measures_only_that_workload(tmp_path):
    code = hash_baseline.main([
        "--build-dir", str(BUILD_DIR), "--machine", "m",
        "--load-condition", "l", "--repetitions", "1", "--scale", "0.002",
        "--workload", "native-shared-library", "--out-dir", str(tmp_path),
        "--label", "one"])
    assert code == 0
    document = json.loads((tmp_path / "hash-baseline-one.json").read_text())
    assert list(document["workloads"]) == ["native-shared-library"]


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def _revision_repo(path):
    """A repository whose two revisions build a differently stamped marker."""
    path.mkdir()
    _git(path, "init", "-q")
    revisions = []
    for marker in ("first", "second"):
        (path / "marker.txt").write_text(marker)
        (path / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\nproject(p NONE)\n"
            "file(READ marker.txt M)\n"
            'file(WRITE ${CMAKE_BINARY_DIR}/built.txt "${M}")\n')
        _git(path, "add", ".")
        _git(path, "commit", "-qm", marker)
        revisions.append(subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
            text=True, check=True).stdout.strip())
    return revisions


def test_a_revision_builds_from_its_own_bytes_outside_the_checkout(tmp_path):
    repo = tmp_path / "repo"
    first, second = _revision_repo(repo)
    (repo / "marker.txt").write_text("uncommitted")
    build = hash_baseline.build_revision(first[:12], tmp_path / "cache", repo)
    assert (build / "built.txt").read_text() == "first"
    assert first in str(build)
    assert not str(build).startswith(str(repo))
    other = hash_baseline.build_revision(second, tmp_path / "cache", repo)
    assert (other / "built.txt").read_text() == "second"
    # The checkout, its branch and its worktree list stay as they were.
    assert (repo / "marker.txt").read_text() == "uncommitted"
    listed = subprocess.run(["git", "worktree", "list"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert len(listed.splitlines()) == 1


def test_a_completed_revision_build_is_reused(tmp_path):
    repo = tmp_path / "repo"
    first, _ = _revision_repo(repo)
    build = hash_baseline.build_revision(first, tmp_path / "cache", repo)
    (build / "built.txt").write_text("kept")
    again = hash_baseline.build_revision(first, tmp_path / "cache", repo)
    assert again == build
    assert (again / "built.txt").read_text() == "kept"


def test_an_unknown_revision_is_a_clear_error(tmp_path):
    repo = tmp_path / "repo"
    _revision_repo(repo)
    with pytest.raises(hash_baseline.SelectionError, match="nosuchrev"):
        hash_baseline.build_revision("nosuchrev", tmp_path / "cache", repo)


def test_a_debug_build_is_refused_as_baseline_evidence(tmp_path):
    (tmp_path / "CMakeCache.txt").write_text(
        f"CMAKE_BUILD_TYPE:STRING=Debug\nCMAKE_HOME_DIRECTORY:INTERNAL={ROOT}\n")
    with pytest.raises(hash_baseline.SelectionError, match="Release"):
        hash_baseline.select_build(tmp_path)
