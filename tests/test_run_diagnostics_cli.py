"""`sil run --diagnostics`: the diagnostics runner behind the `sil` command (#302).

The command is run as its own process, with a PATH that holds either the
built native binaries or a fake runner, so these tests see what a user sees:
exit code, stdout, stderr and the files left behind. No assertion depends on
how long anything took.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT
from sil.diagnostics_report import validate_run_report
from sil.diagnostics_run import format_summary
from test_run_diagnostics import (
    failing_step_manifest, mixed_period_manifest, wedged_manifest,
)

FIXTURES = ROOT / "tests" / "fixtures" / "diagnostics"
NATIVE = ("sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher")


@pytest.fixture
def native_path(build_dir, tmp_path) -> Path:
    """A PATH directory with only the built native binaries in it."""
    directory = tmp_path / "native bin"
    directory.mkdir()
    for name in NATIVE:
        (directory / name).symlink_to(build_dir / name)
    return directory


def sil(argv, path: Path, cwd: Path, **kwargs):
    """`sil` as its own process, finding native tools only in `path`."""
    env = {**os.environ, "PATH": os.pathsep.join([str(path), "/usr/bin:/bin"])}
    return subprocess.run([sys.executable, "-m", "sil.cli", *argv],
                          capture_output=True, text=True, cwd=cwd, env=env,
                          **kwargs)


def test_diagnostics_write_the_default_report_and_print_a_summary(
    native_path, tmp_path
):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(["run", str(manifest), "--diagnostics", "-o", "run.mcap"],
               native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    report = json.loads((tmp_path / "run.mcap.diagnostics.json").read_text())
    assert validate_run_report(report) == []
    lines = proc.stdout.splitlines()
    # The runner's own stdout comes first and is unchanged.
    assert lines[0] == f"manifest_hash {report['identity']['manifest_hash']}"
    assert "diagnostics report: run.mcap.diagnostics.json" in lines
    assert "run: succeeded, exit code 0, complete" in lines
    assert proc.stderr == ""


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_summary_names_units_scopes_and_attribution():
    text = format_summary(fixture("run-success.json"), Path("run.json"))

    assert text == """\
diagnostics report: run.json
run: succeeded, exit code 0, complete
run wall time: 67.235 ms (native runner start before Manifest load through \
Participant cleanup and Recording close)
phases: preflight 8.079 ms, setup 38.608 ms, scheduled execution 202.083 us, \
shutdown 20.237 ms, recording finalization 108.542 us, unattributed 0 ns
virtual-time speed: scheduled 98.97x, end-to-end 0.30x
throughput per second of scheduled execution (202.083 us): \
24 native activations, 118763.08/s; 2 process steps, 9896.92/s
tasks (inclusive wall time of completed activations):
  multi/fast native_task: 20 of 20 completed, mean 1.215 us, p50 208 ns, \
p95 542 ns, p99 unavailable (insufficient_samples), max 19.583 us
  multi/slow native_task: 4 of 4 completed, mean 302 ns, p50 167 ns, \
p95 unavailable (insufficient_samples), p99 unavailable \
(insufficient_samples), max 625 ns
  pyecho/step process_step: 2 of 2 completed, mean 75.854 us, p50 37.958 us, \
p95 unavailable (insufficient_samples), p99 unavailable \
(insufficient_samples), max 113.750 us
resources (peak RSS is per process and never summed):
  runner: user CPU 41.000 ms, system CPU 12.000 ms, peak RSS 9.000 MiB \
(process_peak_rss; Native participants run in the runner)
  process pyecho: user CPU 180.000 ms, system CPU 30.000 ms, \
peak RSS 14.000 MiB (largest_process_peak_rss; includes waited descendants)
  CPU total of runner and Process participants: user 221.000 ms, \
system 42.000 ms
  limitations: runner peak_includes_parent_rss_at_spawn; pyecho \
peak_includes_parent_rss_at_spawn, descendants_counted_only_when_waited
"""


def test_summary_of_a_failed_run_is_marked_partial():
    lines = format_summary(fixture("run-failed-partial.json"),
                           Path("run.json")).splitlines()

    assert "run: run_failure, exit code 1, partial (run_failure)" in lines
    assert ("failure: participant 'test' failed: AssertionError: boom at "
            "t=20000000 (participant, Participant test, virtual time "
            "20.000 ms)") in lines
    assert "last virtual time: 20.000 ms of Duration 50.000 ms" in lines
    assert ("virtual-time speed: scheduled unavailable (run_not_successful), "
            "end-to-end unavailable (run_not_successful)") in lines
    assert any(line.startswith(
        "  test/step process_step: 2 of 3 completed, 1 failed (first at "
        "activation 2, virtual time 20.000 ms), mean ") for line in lines)


def test_summary_without_samples_or_resources_shows_reasons_not_zeros():
    zero = format_summary(fixture("run-zero-activations.json"),
                          Path("r.json")).splitlines()
    assert ("  late/tick native_task: 0 of 0 completed, timing unavailable "
            "(no_completed_activations)") in zero

    unsupported = format_summary(fixture("run-unsupported-metric.json"),
                                 Path("r.json")).splitlines()
    assert ("  runner: user CPU unavailable (unsupported_platform), system "
            "CPU unavailable (unsupported_platform), peak RSS unavailable "
            "(unsupported_platform)") in unsupported
    assert ("  CPU total of runner and Process participants: user "
            "unavailable (incomplete_scopes), system unavailable "
            "(incomplete_scopes)") in unsupported
