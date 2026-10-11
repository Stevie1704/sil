"""`sil check` keeps the two Recordings and reports where they first differ
(issue #271)."""

import json
import subprocess
import sys

from conftest import ROOT
from toys import toy_manifest

STEP_NS = 10_000_000


def nondet_manifest(tmp_path):
    """A Process participant that publishes entropy on 'echo' every Step."""
    m = toy_manifest(duration_ns=50_000_000)
    m.add_channel("echo", schema="toy.Counter")
    m.add_process(
        "noisy",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "nondet.py")],
        step_period_ns=STEP_NS,
        publishes=["echo"],
    )
    return m.write(tmp_path / "nondet.json").path


def run_check(sil_run, manifest, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "sil.check", str(manifest),
         "--runner", str(sil_run), *arguments],
        capture_output=True, text=True,
    )


def test_a_violation_names_the_channel_and_time_of_the_first_different_message(
        sil_run, tmp_path):
    proc = run_check(sil_run, nondet_manifest(tmp_path), "--json")

    assert proc.returncode == 3, proc.stderr
    report = json.loads(proc.stdout)
    assert report["sil_check_report"] == 1
    assert report["verdict"] == "violation"
    first = report["first_difference"]
    assert first["kind"] == "message"
    assert first["channel"] == "echo"
    assert first["t_ns"] == 0
    assert first["message_index"] == 0


def test_the_violation_text_names_the_first_different_message(sil_run, tmp_path):
    proc = run_check(sil_run, nondet_manifest(tmp_path))

    assert proc.returncode == 3
    assert "DETERMINISM VIOLATION" in proc.stderr
    assert ("first difference: Message 0 in stored order, on Channel 'echo' "
            "at t_ns 0, has a different payload") in proc.stderr


def test_keep_leaves_both_recordings_in_the_directory(sil_run, tmp_path):
    kept = tmp_path / "kept"

    proc = run_check(sil_run, nondet_manifest(tmp_path), "--keep", str(kept),
                     "--json")

    assert proc.returncode == 3, proc.stderr
    runs = json.loads(proc.stdout)["runs"]
    assert [run["recording"] for run in runs] == [
        str(kept / "run1.mcap"), str(kept / "run2.mcap")]
    assert (kept / "run1.mcap").read_bytes() != (kept / "run2.mcap").read_bytes()


def test_without_keep_the_report_names_no_recording(sil_run, tmp_path):
    proc = run_check(sil_run, nondet_manifest(tmp_path), "--json")

    assert [run["recording"] for run in json.loads(proc.stdout)["runs"]] == [
        None, None]


def test_an_unreadable_recording_is_still_a_violation(tmp_path):
    # A runner stand-in that writes different bytes, not MCAP, on each call.
    runner = tmp_path / "stub-run"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "out.write_bytes(os.urandom(16))\n")
    runner.chmod(0o755)
    manifest = tmp_path / "m.json"
    manifest.write_text("{}")

    proc = run_check(runner, manifest, "--json")

    assert proc.returncode == 3, proc.stderr
    first = json.loads(proc.stdout)["first_difference"]
    assert first["kind"] == "unreadable"
    assert "cannot read" in first["detail"]
