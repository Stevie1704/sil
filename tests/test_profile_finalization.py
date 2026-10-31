"""The artifact inventory of a profile directory is built once (#328).

These tests call the command in this process, with the fake perf of
test_profile_run, so that they can count each hash of the profile tool. The
expected digests come from `hashlib` over the files on disk.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import platform
from collections import Counter
from pathlib import Path

import pytest

from sil import profile_common, profile_run
from sil.profile_activation import Correlation
from test_profile_heap_run import heaptrack, marker_manifest  # noqa: F401
from test_profile_run import exports, tools  # noqa: F401  (fixtures)
from test_run_diagnostics import failing_step_manifest, mixed_period_manifest


@pytest.fixture
def capture(tools, exports, tmp_path, monkeypatch):  # noqa: F811
    """Runs `sil profile`; gives its report and each path that it hashed."""
    for name, value in exports.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    monkeypatch.chdir(tmp_path)
    hashed = Counter()
    real = profile_common.file_sha256

    def counted(path: Path) -> str:
        hashed[Path(path).resolve()] += 1
        return real(path)

    monkeypatch.setattr(profile_common, "file_sha256", counted)

    def run(manifest: Path, *options: str) -> dict:
        profile_run.main([str(manifest), "--out", "prof", "--collector", "cpu",
                          *options])
        return json.loads((tmp_path / "prof" / "profile.json").read_text())

    return run, hashed


def assert_inventory_hashed_once(report: dict, out: Path, hashed: Counter):
    rows = {(out / row["path"]).resolve(): row for row in report["artifacts"]}
    assert rows
    assert hashed == Counter(dict.fromkeys(rows, 1))
    for path, row in rows.items():
        assert row["bytes"] == path.stat().st_size
        assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def correlation_document(monkeypatch) -> None:
    """A correlation that writes its file, which the fake perf cannot prove."""
    real = profile_run.correlate

    def with_document(*args, **kwargs) -> Correlation:
        return dataclasses.replace(real(*args, **kwargs),
                                   document={"activations": []})

    monkeypatch.setattr(profile_run, "correlate", with_document)


def test_activations_hash_each_finalized_artifact_once(
        capture, tmp_path, monkeypatch):
    run, hashed = capture
    correlation_document(monkeypatch)

    report = run(mixed_period_manifest(tmp_path), "--activations")

    assert_inventory_hashed_once(report, tmp_path / "prof", hashed)
    roles = {row["role"] for row in report["artifacts"]}
    # A row exists for a file that only correlation writes, so the inventory
    # was built after correlation.
    assert {"recording", "activation_observations",
            "activation_correlation"} <= roles


def test_no_activations_keep_the_inventory(capture, tmp_path):
    run, hashed = capture

    report = run(mixed_period_manifest(tmp_path))

    assert_inventory_hashed_once(report, tmp_path / "prof", hashed)
    assert {"diagnostics_report", "recording", "perf_data",
            "cpu_samples"} <= {row["role"] for row in report["artifacts"]}


def test_external_mode_keeps_the_profile_digest_of_the_recording(
        capture, tmp_path):
    run, hashed = capture

    report = run(mixed_period_manifest(tmp_path),
                 "--provenance-mode", "external")

    out = tmp_path / "prof"
    assert_inventory_hashed_once(report, out, hashed)
    assert "recording" in {row["role"] for row in report["artifacts"]}
    side_car = json.loads((out / "run.mcap.provenance.json").read_text())
    assert side_car["recording"] == {"digest_state": "omitted", "sha256": None}


def test_full_mode_hashes_the_recording_apart_from_the_runner(
        capture, tmp_path):
    """No handoff: the profile tool hashes the file in its finalized state, and
    the two digests agree because they are of the same bytes."""
    run, hashed = capture

    report = run(mixed_period_manifest(tmp_path))

    out = tmp_path / "prof"
    recording = (out / "run.mcap").resolve()
    assert hashed[recording] == 1
    side_car = json.loads((out / "run.mcap.provenance.json").read_text())
    row = next(r for r in report["artifacts"] if r["role"] == "recording")
    assert side_car["recording"]["sha256"] == row["sha256"]


def test_a_failed_run_keeps_the_digest_of_its_partial_recording(
        capture, tmp_path):
    run, hashed = capture

    report = run(failing_step_manifest(tmp_path))

    out = tmp_path / "prof"
    assert report["run"]["status"] == "run_failure"
    assert_inventory_hashed_once(report, out, hashed)
    side_car = json.loads((out / "run.mcap.provenance.json").read_text())
    row = next(r for r in report["artifacts"] if r["role"] == "recording")
    assert side_car["recording"]["sha256"] == row["sha256"]


@pytest.mark.skipif(platform.system() != "Linux",
                    reason="Heaptrack is Linux only")
@pytest.mark.parametrize("mode", ["full", "external"])
def test_a_heap_capture_hashes_each_artifact_once(
        tools, heaptrack, tmp_path, monkeypatch, capture, mode):  # noqa: F811
    _, hashed = capture
    manifest, _ = marker_manifest(tmp_path)
    for name, value in heaptrack.items():
        monkeypatch.setenv(name, value)

    profile_run.main([str(manifest), "--out", "prof", "--collector", "heap",
                      "--runner", "--provenance-mode", mode])

    out = tmp_path / "prof"
    report = json.loads((out / "profile.json").read_text())
    assert_inventory_hashed_once(report, out, hashed)
    assert {"recording", "heap_allocations"} <= {
        row["role"] for row in report["artifacts"]}


def test_no_recording_has_no_recording_artifact(capture, tmp_path):
    run, hashed = capture

    report = run(mixed_period_manifest(tmp_path), "--no-recording")

    assert_inventory_hashed_once(report, tmp_path / "prof", hashed)
    assert "recording" not in {row["role"] for row in report["artifacts"]}


def test_a_recording_outside_the_profile_directory_is_no_artifact(
        capture, tmp_path):
    run, hashed = capture

    report = run(mixed_period_manifest(tmp_path), "-o", "outside.mcap")

    assert "recording" not in {row["role"] for row in report["artifacts"]}
    assert (tmp_path / "outside.mcap").resolve() not in hashed
