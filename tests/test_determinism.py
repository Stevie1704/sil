"""Milestone exit criterion: run twice -> bit-identical MCAP, and the
determinism check catches participants that break the contract."""

import hashlib
import subprocess
import sys

from conftest import ROOT
from toys import add_accumulator, add_producer, toy_manifest


def full_pipeline_manifest(tmp_path):
    """Native producer -> native accumulator -> python process echo."""
    m = toy_manifest(duration_ns=100_000_000)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("sums", schema="toy.Accum")
    m.add_channel("echo", schema="toy.Counter")
    add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
    add_accumulator(
        m,
        "accumulator",
        input_channel="ticks",
        output_channel="sums",
        period_ns=20_000_000,
    )
    m.add_process(
        "pyecho",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "echo.py")],
        step_period_ns=10_000_000,
        subscribes=["ticks"],
        publishes=["echo"],
    )
    return m.write(tmp_path / "pipeline.json")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_two_runs_are_bit_identical(run_sil, tmp_path):
    ref = full_pipeline_manifest(tmp_path)
    a = run_sil(ref.path, out=tmp_path / "a.mcap")
    b = run_sil(ref.path, out=tmp_path / "b.mcap")
    assert a.returncode == 0, a.stderr
    assert b.returncode == 0, b.stderr
    assert sha256(a.mcap_path) == sha256(b.mcap_path)


class TestCheckCli:
    def run_check(self, sil_run, manifest_path):
        return subprocess.run(
            [sys.executable, "-m", "sil.check", str(manifest_path),
             "--runner", str(sil_run)],
            capture_output=True, text=True,
        )

    def test_reports_deterministic_pipeline(self, sil_run, tmp_path):
        ref = full_pipeline_manifest(tmp_path)
        proc = self.run_check(sil_run, ref.path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("deterministic: ")

    def test_flags_nondeterministic_participant(self, sil_run, tmp_path):
        m = toy_manifest(duration_ns=50_000_000)
        m.add_channel("echo", schema="toy.Counter")
        m.add_process(
            "noisy",
            command=[sys.executable,
                     str(ROOT / "tests" / "participants" / "nondet.py")],
            step_period_ns=10_000_000,
            publishes=["echo"],
        )
        ref = m.write(tmp_path / "nondet.json")
        proc = self.run_check(sil_run, ref.path)
        assert proc.returncode == 3
        assert "DETERMINISM VIOLATION" in proc.stderr

    def test_propagates_run_failure(self, sil_run, tmp_path):
        m = toy_manifest(duration_ns=50_000_000)
        m.add_process(
            "test",
            command=[sys.executable,
                     str(ROOT / "tests" / "participants" / "fail_at_20ms.py")],
            step_period_ns=10_000_000,
        )
        ref = m.write(tmp_path / "failing.json")
        proc = self.run_check(sil_run, ref.path)
        assert proc.returncode == 1
        assert "boom" in proc.stderr
