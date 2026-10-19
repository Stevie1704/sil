"""`sil-diagnostics-launcher`: the minimal process a supervisor starts the
diagnostics runner through (#301).

On Linux a peak resident value survives exec: a process forked from a large
Python supervisor starts with the supervisor's resident size as its high-water
mark. The launcher is small when it forks, so the runner's own record measures
the Run. It also forwards termination signals and passes the runner's outcome
through unchanged.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from conftest import ROOT
from sil.diagnostics_report import validate_run_report
from toys import add_producer, toy_manifest

MS = 1_000_000
TIMEOUT_PARTICIPANT = ROOT / "tests" / "participants" / "timeout.py"


def native_manifest(tmp_path: Path) -> Path:
    m = toy_manifest(duration_ns=10 * MS)
    m.add_channel("ticks", schema="toy.Counter")
    add_producer(m, "prod", channel="ticks", period_ns=MS)
    return m.write(tmp_path / "native.json").path


def test_runner_outcome_and_report_pass_through(
    sil_diagnostics_launcher, sil_run_diagnostics, tmp_path
):
    report = tmp_path / "run.json"
    proc = subprocess.run(
        [str(sil_diagnostics_launcher), str(sil_run_diagnostics),
         str(native_manifest(tmp_path)), "-o", str(tmp_path / "run.mcap"),
         "--diagnostics-out", str(report)],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    # The runner's stdout reaches the supervisor unchanged.
    assert proc.stdout.startswith("manifest_hash ")
    document = json.loads(report.read_text())
    assert validate_run_report(document) == []


@pytest.mark.parametrize("code", [1, 2, 7])
def test_exit_code_is_the_runners(sil_diagnostics_launcher, code):
    proc = subprocess.run(
        [str(sil_diagnostics_launcher), "/bin/sh", "-c", f"exit {code}"],
        capture_output=True, text=True,
    )
    assert proc.returncode == code
    assert proc.stderr == ""


def test_runner_ended_by_a_signal_ends_the_launcher_the_same_way(
    sil_diagnostics_launcher,
):
    proc = subprocess.run(
        [str(sil_diagnostics_launcher), "/bin/sh", "-c", "kill -KILL $$"],
        capture_output=True, text=True,
    )
    assert proc.returncode == -signal.SIGKILL


def test_missing_runner_exits_127(sil_diagnostics_launcher, tmp_path):
    missing = tmp_path / "no-such-runner"
    proc = subprocess.run(
        [str(sil_diagnostics_launcher), str(missing)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 127
    assert proc.stderr.startswith("sil-diagnostics-launcher: cannot run '")
    assert str(missing) in proc.stderr


def read_all(fd: int) -> str:
    with os.fdopen(fd, "rb") as handle:
        return handle.read().decode()


def test_status_fd_carries_the_launchers_own_failure(
    sil_diagnostics_launcher, tmp_path
):
    missing = tmp_path / "no-such-runner"
    read_end, write_end = os.pipe()
    proc = subprocess.run(
        [str(sil_diagnostics_launcher), "--status-fd", str(write_end),
         str(missing)],
        capture_output=True, text=True, pass_fds=(write_end,),
    )
    os.close(write_end)
    assert proc.returncode == 127
    assert proc.stderr == ""
    status = read_all(read_end)
    assert status.startswith("sil-diagnostics-launcher: cannot run '")
    assert str(missing) in status


def test_the_runner_does_not_inherit_the_status_fd(sil_diagnostics_launcher):
    read_end, write_end = os.pipe()
    probe = "import os, sys; os.fstat(int(sys.argv[1]))"
    proc = subprocess.run(
        [str(sil_diagnostics_launcher), "--status-fd", str(write_end),
         sys.executable, "-c", probe, str(write_end)],
        capture_output=True, text=True, pass_fds=(write_end,),
    )
    os.close(write_end)
    assert proc.returncode == 1, proc.stderr
    assert "Bad file descriptor" in proc.stderr
    assert read_all(read_end) == ""


@pytest.mark.parametrize("argv", [["--status-fd"], ["--status-fd", "x", "sh"],
                                  ["--status-fd", "-1", "sh"],
                                  ["--status-fd", "999", "sh"]])
def test_an_invalid_status_fd_is_a_usage_error(sil_diagnostics_launcher,
                                               argv):
    proc = subprocess.run([str(sil_diagnostics_launcher), *argv],
                          capture_output=True, text=True)
    assert proc.returncode == 2
    assert "usage: sil-diagnostics-launcher " in proc.stderr


def test_usage_error_exits_2(sil_diagnostics_launcher):
    proc = subprocess.run(
        [str(sil_diagnostics_launcher)], capture_output=True, text=True
    )
    assert proc.returncode == 2
    assert ("usage: sil-diagnostics-launcher [--status-fd N] "
            "[--environment-fd M] RUNNER [ARG...]") in proc.stderr


def test_the_runner_gets_the_environment_of_the_descriptor(
        sil_diagnostics_launcher, tmp_path):
    environment = tmp_path / "environment"
    environment.write_bytes(b"ONLY=this\0PATH=/usr/bin:/bin\0")
    with environment.open("rb") as stream:
        proc = subprocess.run(
            [str(sil_diagnostics_launcher), "--environment-fd",
             str(stream.fileno()), "/usr/bin/env"],
            capture_output=True, text=True, pass_fds=(stream.fileno(),),
            env={**os.environ, "DROPPED": "1"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["ONLY=this", "PATH=/usr/bin:/bin"]


def test_an_unreadable_environment_descriptor_is_a_usage_error(
        sil_diagnostics_launcher):
    proc = subprocess.run(
        [str(sil_diagnostics_launcher), "--environment-fd", "999",
         "/usr/bin/true"], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "cannot read the environment" in proc.stderr


@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
def test_signal_to_the_launcher_interrupts_the_run_and_its_participants(
    sil_diagnostics_launcher, sil_run_diagnostics, tmp_path, signal_number
):
    observer = tmp_path / "observer.txt"
    m = toy_manifest(duration_ns=50 * MS)
    m.add_process(
        "wedged",
        command=[sys.executable, str(TIMEOUT_PARTICIPANT), "step",
                 str(observer)],
        step_period_ns=10 * MS,
    )
    report = tmp_path / "run.json"
    # A new session: the signal reaches the launcher only, as when a
    # supervisor signals the process it started.
    launcher = subprocess.Popen(
        [str(sil_diagnostics_launcher), str(sil_run_diagnostics),
         str(m.write(tmp_path / "wedged.json").path),
         "-o", str(tmp_path / "run.mcap"), "--diagnostics-out", str(report)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    # The participant writes <observer>.step when the first Step arrives:
    # interrupt that Step, not the startup.
    step_marker = Path(f"{observer}.step")
    deadline = time.monotonic() + 10
    while not step_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert step_marker.exists(), "the first Step did not arrive"
    participant_pid = int(observer.read_text().split()[0])
    launcher.send_signal(signal_number)
    _, stderr = launcher.communicate(timeout=15)
    assert launcher.returncode == 1, stderr
    assert "run interrupted" in stderr
    document = json.loads(report.read_text())
    assert document["run"]["status"] == "interrupted"
    # The interrupted Participant was reaped, and its evidence kept.
    with pytest.raises(ProcessLookupError):
        os.kill(participant_pid, 0)
    (record,) = document["observational"]["resources"]["process_participants"]
    if sys.platform in ("linux", "darwin"):
        assert record["peak_rss_bytes"] > 0


SUPERVISOR = textwrap.dedent("""
    import json, subprocess, sys
    mib, launcher, runner, manifest, workdir = sys.argv[1:6]
    size = int(mib) * 1024 * 1024
    ballast = bytearray(size)
    for offset in range(0, size, 4096):
        ballast[offset] = 1
    peaks = {}
    for label, prefix in (("direct", []), ("launched", [launcher])):
        report = f"{workdir}/{label}.json"
        subprocess.run(
            prefix + [runner, manifest, "-o", f"{workdir}/{label}.mcap",
                      "--diagnostics-out", report],
            check=True, capture_output=True, cwd=workdir)
        with open(report) as handle:
            document = json.load(handle)
        peaks[label] = (
            document["observational"]["resources"]["runner"]["peak_rss_bytes"])
    print(json.dumps(peaks))
""")


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="ru_maxrss survives exec only on Linux")
def test_memory_heavy_supervisor_does_not_reach_the_runner_record(
    sil_diagnostics_launcher, sil_run_diagnostics, tmp_path
):
    """The same supervisor starts the same Run twice: directly, the runner
    inherits the supervisor's high-water mark; through the launcher it does
    not. The ballast is the control, not a machine-specific threshold."""
    mib = 512
    proc = subprocess.run(
        [sys.executable, "-c", SUPERVISOR, str(mib),
         str(sil_diagnostics_launcher), str(sil_run_diagnostics),
         str(native_manifest(tmp_path)), str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    peaks = json.loads(proc.stdout)
    ballast = mib * 2**20
    assert peaks["direct"] >= ballast, peaks
    assert peaks["launched"] < ballast, peaks
