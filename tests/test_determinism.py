"""Milestone exit criterion: run twice -> bit-identical MCAP, and the
determinism check catches participants that break the contract.

The same fixtures carry the inertness invariant the counter surface rests on
(issue #82): observing a Run under `sil-run-instrumented` must produce the
same exit code and the same Recording bytes as the production runner, or
re-running a Run instrumented is no longer a way to answer a question about it.
"""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

import pytest

from conftest import COMPAT_ROUTE_CAPACITY, ROOT, run_manifest
from toys import (
    add_accumulator,
    add_producer,
    bounded_route_manifest,
    toy_manifest,
)
from sil.manifest import SubscriberRoute


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
        subscribes=[SubscriberRoute("ticks", capacity=COMPAT_ROUTE_CAPACITY)],
        publishes=["echo"],
    )
    return m.write(tmp_path / "pipeline.json")


def drop_newest_manifest(tmp_path):
    """A full bounded route that drops rather than fails: it runs to the end."""
    m = bounded_route_manifest(capacity=2, overflow="drop_newest")
    return m.write(tmp_path / "drop-newest.json")


def overflow_abort_manifest(tmp_path):
    """A full bounded route that fails the Run partway through it."""
    return bounded_route_manifest(capacity=2).write(tmp_path / "overflow.json")


def participant_failure_manifest(tmp_path):
    """A process participant that aborts the Run from inside a step."""
    m = toy_manifest(duration_ns=50_000_000)
    m.add_process(
        "test",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "fail_at_20ms.py")],
        step_period_ns=10_000_000,
    )
    return m.write(tmp_path / "failing.json")


@dataclass(frozen=True)
class Fixture:
    build: Callable
    exit_code: int


# Every fixture the determinism contract is asserted over. A failing Run is as
# much a determinism fixture as a clean one: it writes a partial Recording that
# has to reproduce too, and it is the case where instrumentation could most
# easily change what a reader sees.
FIXTURES = {
    "full_pipeline": Fixture(full_pipeline_manifest, exit_code=0),
    "drop_newest": Fixture(drop_newest_manifest, exit_code=0),
    "overflow_abort": Fixture(overflow_abort_manifest, exit_code=1),
    "participant_failure": Fixture(participant_failure_manifest, exit_code=1),
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_two_runs_are_bit_identical(name, sil_run, tmp_path):
    fixture = FIXTURES[name]
    ref = fixture.build(tmp_path)
    a = run_manifest(sil_run, ref.path, tmp_path / "a.mcap")
    b = run_manifest(sil_run, ref.path, tmp_path / "b.mcap")
    assert a.returncode == b.returncode == fixture.exit_code, a.stderr
    assert sha256(a.mcap_path) == sha256(b.mcap_path)


class TestInstrumentedRunnerIsInert:
    """Observing a Run must not change it (issues #46, #63, #82)."""

    @pytest.mark.parametrize("name", sorted(FIXTURES))
    def test_both_runners_agree_on_exit_code_and_recording(
        self, name, sil_run, sil_run_instrumented, tmp_path
    ):
        fixture = FIXTURES[name]
        ref = fixture.build(tmp_path)
        report = tmp_path / "report.json"
        env = dict(os.environ, SIL_COPY_COUNTERS_OUT=str(report))

        production = run_manifest(sil_run, ref.path, tmp_path / "production.mcap")
        instrumented = run_manifest(
            sil_run_instrumented, ref.path, tmp_path / "instrumented.mcap", env
        )

        assert production.returncode == fixture.exit_code, production.stderr
        assert instrumented.returncode == production.returncode
        # Byte-identity is also what keeps every counter out of the Recording:
        # one MCAP metadata record carrying a count would break it.
        assert (
            instrumented.mcap_path.read_bytes()
            == production.mcap_path.read_bytes()
        )
        assert json.loads(report.read_text())["run_exit_code"] == (
            production.returncode
        )

    def test_an_unwritable_report_changes_no_exit_code_or_diagnostic(
        self, sil_run, sil_run_instrumented, tmp_path
    ):
        fixture = FIXTURES["overflow_abort"]
        ref = fixture.build(tmp_path)
        env = dict(
            os.environ,
            SIL_COPY_COUNTERS_OUT=str(tmp_path / "absent" / "report.json"),
        )

        production = run_manifest(sil_run, ref.path, tmp_path / "production.mcap")
        instrumented = run_manifest(
            sil_run_instrumented, ref.path, tmp_path / "instrumented.mcap", env
        )

        # Pinned, so this stays a failed Run's report path rather than passing
        # vacuously if the fixture ever stopped failing the way it does.
        assert production.returncode == fixture.exit_code
        assert instrumented.returncode == production.returncode
        # The report's own failure is appended after the Run's diagnostic, so
        # the authoritative first message is untouched.
        assert instrumented.stderr.startswith(production.stderr)
        assert "cannot write copy counters" in instrumented.stderr


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
        ref = participant_failure_manifest(tmp_path)
        proc = self.run_check(sil_run, ref.path)
        assert proc.returncode == 1
        assert "boom" in proc.stderr
