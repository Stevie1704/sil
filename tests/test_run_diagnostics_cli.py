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
from sil import diagnostics_run
from sil.diagnostics_run import (
    NativeTools, RunRequest, execute, format_summary,
)
from test_process_descendants import (
    _wait_for_observer, assert_cleanup, descendant_manifest,
)
from test_run_diagnostics import (
    MS, failing_step_manifest, mixed_period_manifest, wedged_manifest,
)
from toys import add_producer, toy_manifest

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
provenance mode: full
run wall time: 67.235 ms (native runner start before Manifest load through \
Participant cleanup and Recording close)
phases: preflight 8.079 ms, setup 38.608 ms, scheduled execution 202.083 us, \
shutdown 20.237 ms, recording finalization 108.542 us, unattributed 0 ns
Virtual-time speed: scheduled 98.97x, end-to-end 0.30x
throughput per second of scheduled execution (202.083 us): \
24 Native Activations, 118763.08/s; 2 Process Steps, 9896.92/s
Tasks (inclusive wall time of completed Activations):
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
            "t=20000000 (participant, Participant test, Virtual time "
            "20.000 ms)") in lines
    assert "last Virtual time: 20.000 ms of Duration 50.000 ms" in lines
    assert ("Virtual-time speed: scheduled unavailable (run_not_successful), "
            "end-to-end unavailable (run_not_successful)") in lines
    assert any(line.startswith(
        "  test/step process_step: 2 of 3 completed, 1 failed (first at "
        "Activation 2, Virtual time 20.000 ms), mean ") for line in lines)


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


def test_custom_path_with_spaces_and_no_recording(native_path, tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    results = tmp_path / "my results"
    results.mkdir()

    proc = sil(["run", str(manifest), "--no-recording",
                "--diagnostics-out", "my results/run one.json"],
               native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    report = json.loads((results / "run one.json").read_text())
    assert report["configuration"]["recording"] is False
    assert report["artifacts"]["recording"] is None
    assert "diagnostics report: my results/run one.json" in proc.stdout
    assert ("recording finalization unavailable (no_recording)"
            in proc.stdout)
    assert not (tmp_path / "out.mcap").exists()


def test_no_recording_default_report_is_out_mcap_diagnostics(
    native_path, tmp_path
):
    proc = sil(["run", str(mixed_period_manifest(tmp_path)), "--no-recording",
                "--diagnostics"], native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out.mcap.diagnostics.json").is_file()


def test_guards_are_forwarded_to_the_runner(native_path, tmp_path):
    proc = sil(["run", str(mixed_period_manifest(tmp_path)), "--diagnostics",
                "-o", "run.mcap", "--participant-timeout-ms", "60000",
                "--max-protocol-line-bytes", "1048576",
                "--max-step-output-messages", "64",
                "--max-step-inline-payload-bytes", "65536"],
               native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    configuration = json.loads(
        (tmp_path / "run.mcap.diagnostics.json").read_text())["configuration"]
    assert configuration["participant_timeout_ms"] == 60000
    assert configuration["max_protocol_line_bytes"] == 1048576
    assert configuration["max_step_output_messages"] == 64
    assert configuration["max_step_inline_payload_bytes"] == 65536


@pytest.mark.parametrize("argv, message", [
    (["--html", "a.html", "--html", "b.html"], "may be given only once"),
    (["--diagnostics", "--no-recording", "-o", "x.mcap"],
     "-o and --no-recording exclude each other"),
    (["--diagnostics", "--participant-timeout-ms", "0"],
     "is not a positive integer"),
    (["--diagnostics", "--participant-timeout-ms", "5",
      "--participant-timeout-ms", "6"], "may be given only once"),
    (["--diagnostics", "--max-step-output-messag", "6"],
     "unrecognized arguments"),
])
def test_invalid_usage_exits_2_before_any_run(native_path, tmp_path, argv,
                                              message):
    proc = sil(["run", "manifest.json", *argv], native_path, tmp_path)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert list(tmp_path.iterdir()) == [native_path]


def observed_manifest(tmp_path: Path) -> tuple[Path, Path]:
    """A Manifest whose Process participant leaves a file when it starts."""
    observer = tmp_path / "started.txt"
    return wedged_manifest(tmp_path, observer), observer


@pytest.mark.parametrize("arrange, argv, message", [
    (lambda d: (d / "run.json").write_text("{}"),
     ["--diagnostics-out", "run.json"], "already exists"),
    (lambda d: (d / "run.json").symlink_to(d / "nowhere"),
     ["--diagnostics-out", "run.json"], "already exists"),
    (lambda d: None, ["--diagnostics-out", "run.mcap"], "is also the Recording"),
    (lambda d: None, ["--diagnostics-out", "./run.mcap.provenance.json"],
     "is also the provenance record"),
    (lambda d: None, ["--diagnostics-out", "missing/run.json"],
     "does not exist"),
    (lambda d: (d / "alias").symlink_to(d),
     ["--diagnostics-out", "alias/run.mcap"], "is also the Recording"),
    (lambda d: None, ["--diagnostics", "-o", "wedged.json"],
     "is the Manifest"),
])
def test_rejected_destinations_start_no_participant(
    native_path, tmp_path, arrange, argv, message
):
    manifest, observer = observed_manifest(tmp_path)
    arrange(tmp_path)
    if "-o" not in argv:
        argv = [*argv, "-o", "run.mcap"]

    proc = sil(["run", str(manifest), *argv], native_path, tmp_path,
               timeout=60)

    assert proc.returncode == 2, proc.stderr
    assert message in proc.stderr
    assert not observer.exists()
    assert not (tmp_path / "run.mcap").exists()
    assert manifest.read_text().startswith("{")


def test_report_aliasing_the_manifest_is_rejected(native_path, tmp_path):
    manifest, observer = observed_manifest(tmp_path)
    (tmp_path / "link.json").symlink_to(manifest)

    proc = sil(["run", "link.json", "--diagnostics-out", "wedged.json"],
               native_path, tmp_path)

    assert proc.returncode == 2
    assert "already exists" in proc.stderr
    assert not observer.exists()


def test_failed_run_keeps_its_exit_code_stderr_and_partial_summary(
    native_path, tmp_path
):
    proc = sil(["run", str(failing_step_manifest(tmp_path)), "--diagnostics",
                "-o", "run.mcap"], native_path, tmp_path)

    assert proc.returncode == 1
    # The runner's first diagnostic is the first stderr line, unchanged, and
    # names the runner that wrote it.
    assert proc.stderr.startswith(
        "sil-run-diagnostics: participant 'test' failed")
    assert "run: run_failure, exit code 1, partial (run_failure)" in proc.stdout
    assert "manifest_hash" not in proc.stdout


def test_manifest_error_exits_2_with_a_preflight_report(native_path, tmp_path):
    (tmp_path / "broken.json").write_text("{}")

    proc = sil(["run", "broken.json", "--diagnostics", "-o", "run.mcap"],
               native_path, tmp_path)

    assert proc.returncode == 2
    assert "run: configuration_error, exit code 2" in proc.stdout
    assert proc.stderr.startswith("sil-run-diagnostics: manifest error")


def fake_tools(tmp_path: Path, build_dir: Path, runner_script: str,
               launcher_script: str | None = None) -> Path:
    """A PATH with a shell-script runner, and the real or a fake launcher."""
    directory = tmp_path / "fake bin"
    directory.mkdir()
    runner = directory / "sil-run-diagnostics"
    runner.write_text("#!/bin/sh\n" + runner_script)
    runner.chmod(0o755)
    launcher = directory / "sil-diagnostics-launcher"
    if launcher_script is None:
        launcher.symlink_to(build_dir / "sil-diagnostics-launcher")
    else:
        launcher.write_text("#!/bin/sh\n" + launcher_script)
        launcher.chmod(0o755)
    return directory


def test_success_without_a_report_exits_1_and_says_so(build_dir, tmp_path):
    path = fake_tools(tmp_path, build_dir, "echo manifest_hash abc\nexit 0\n")

    proc = sil(["run", "m.json", "--diagnostics"], path, tmp_path)

    assert proc.returncode == 1
    assert proc.stdout == "manifest_hash abc\n"
    assert ("sil run: no diagnostics report was written "
            "('out.mcap.diagnostics.json'); no metrics are available for "
            "this Run\n") == proc.stderr


def test_a_corrupt_report_after_success_exits_1(build_dir, tmp_path):
    script = ('for a; do last="$a"; done\n'
              'printf "{\\"sil_run_diagnostics\\": 1}" > "$last"\nexit 0\n')
    path = fake_tools(tmp_path, build_dir, script)

    proc = sil(["run", "m.json", "--diagnostics-out", "r.json"], path,
               tmp_path)

    assert proc.returncode == 1
    assert "the diagnostics report is not valid" in proc.stderr
    assert "run:" not in proc.stdout


def test_a_failed_run_without_a_report_keeps_its_exit_code(build_dir,
                                                           tmp_path):
    script = ("echo 'sil-run-diagnostics: cannot write diagnostics report: "
              "disk full' >&2\nexit 1\n")
    path = fake_tools(tmp_path, build_dir, script)

    proc = sil(["run", "m.json", "--diagnostics"], path, tmp_path)

    assert proc.returncode == 1
    assert proc.stderr.splitlines() == [
        "sil-run-diagnostics: cannot write diagnostics report: disk full",
        "sil run: no diagnostics report was written "
        "('out.mcap.diagnostics.json'); no metrics are available for this "
        "Run",
    ]


def test_a_hard_crash_is_reported_without_fabricated_metrics(build_dir,
                                                             tmp_path):
    path = fake_tools(tmp_path, build_dir, "kill -KILL $$\n")

    proc = sil(["run", "m.json", "--diagnostics"], path, tmp_path)

    assert proc.returncode == 128 + signal.SIGKILL
    assert ("the diagnostics runner ended by signal SIGKILL (9); no Run exit "
            "code exists") in proc.stderr
    assert "no metrics are available" in proc.stderr
    assert proc.stdout == ""


def test_a_launcher_failure_is_not_mistaken_for_a_run_failure(build_dir,
                                                              tmp_path):
    # The launcher reports its own failure on the --status-fd channel.
    launcher = ('[ "$1" = --status-fd ] || exit 99\n'
                "echo 'sil-diagnostics-launcher: fork failed: Resource "
                "temporarily unavailable' > /dev/fd/$2\nexit 1\n")
    path = fake_tools(tmp_path, build_dir, "exit 0\n", launcher)

    proc = sil(["run", "m.json", "--diagnostics"], path, tmp_path)

    assert proc.returncode == 1
    assert proc.stderr.splitlines() == [
        "sil run: sil-diagnostics-launcher failed, so no Run took place: "
        "fork failed: Resource temporarily unavailable",
    ]


def test_a_launcher_line_on_the_runs_stderr_is_only_copied(build_dir,
                                                           tmp_path):
    """Run stderr, which Participants write to, cannot fake the launcher."""
    fake = "sil-diagnostics-launcher: fork failed: forged by a Participant"
    script = (f"echo '{fake}' >&2\n"
              'for a; do last="$a"; done\n'
              f"cp '{FIXTURES / 'run-success.json'}' \"$last\"\nexit 0\n")
    path = fake_tools(tmp_path, build_dir, script)

    proc = sil(["run", "m.json", "--diagnostics"], path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == fake + "\n"
    assert "run: succeeded, exit code 0, complete" in proc.stdout


class ClosingSink:
    """A stderr sink its owner closes, like a benchmark Run's stderr.log."""

    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ValueError("write to closed file")
        self.data += data

    def flush(self) -> None:
        if self.closed:
            raise ValueError("flush of closed file")


def test_stderr_held_open_by_a_descendant_is_detached_with_a_notice(
    build_dir, tmp_path, monkeypatch
):
    """After the drain time the copy stops writing to the sink and says so,
    so a sink closed after execute() returns is never written to."""
    monkeypatch.setattr(diagnostics_run, "STDERR_DRAIN_S", 0.2)
    marker = tmp_path / "late-written"
    script = ("echo early >&2\n"
              f"(sleep 1; echo late >&2; touch '{marker}') &\nexit 0\n")
    path = fake_tools(tmp_path, build_dir, script)
    sink = ClosingSink()

    outcome = execute(RunRequest(Path("m.json"), Path("r.json")),
                      NativeTools(path / "sil-run-diagnostics",
                                  path / "sil-diagnostics-launcher"),
                      cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=sink)
    sink.closed = True
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert outcome.exit_code == 0
    assert marker.exists()
    time.sleep(0.2)  # the late line has reached the copy thread
    assert sink.data == (
        b"early\n"
        b"sil run: stopped copying the Run's stderr: a process that "
        b"outlived the runner still holds it open\n")


@pytest.mark.parametrize("present, missing", [
    (("sil-diagnostics-launcher",), "sil-run-diagnostics"),
    (("sil-run-diagnostics",), "sil-diagnostics-launcher"),
])
def test_a_missing_native_tool_is_not_found(build_dir, tmp_path, present,
                                            missing):
    directory = tmp_path / "partial bin"
    directory.mkdir()
    for name in present:
        (directory / name).symlink_to(build_dir / name)

    proc = sil(["run", "m.json", "--diagnostics"], directory, tmp_path)

    assert proc.returncode == 127
    assert f"cannot find {missing} on PATH" in proc.stderr


def test_a_signal_while_the_runner_starts_is_forwarded(build_dir, tmp_path,
                                                      monkeypatch):
    """A signal between the spawn and the runner's first instruction is
    held and forwarded, so a runner in its own session cannot outlive it."""
    path = fake_tools(tmp_path, build_dir, "exec sleep 20\n")
    spawn = subprocess.Popen

    def spawn_then_signal(*args, **kwargs):
        process = spawn(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn_then_signal)
    unforwarded: list[int] = []
    outer = signal.signal(signal.SIGTERM,
                          lambda number, _frame: unforwarded.append(number))
    started = time.monotonic()
    try:
        outcome = execute(RunRequest(Path("m.json"), Path("r.json")),
                          NativeTools(path / "sil-run-diagnostics",
                                      path / "sil-diagnostics-launcher"),
                          cwd=tmp_path, stdout=subprocess.DEVNULL,
                          stderr=ClosingSink())
    finally:
        signal.signal(signal.SIGTERM, outer)

    assert unforwarded == []
    assert outcome.supervisor_signal == signal.SIGTERM
    assert outcome.signal == signal.SIGTERM
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_a_signal_to_sil_stops_the_run_and_its_descendants(
    native_path, tmp_path, termination_signal
):
    regions = tmp_path / "regions"
    regions.mkdir()
    observer = tmp_path / "observer.json"
    descendant_manifest("descendant-timeout", observer).write(
        tmp_path / "run.json")
    env = {**os.environ, "TMPDIR": str(regions),
           "PATH": os.pathsep.join([str(native_path), "/usr/bin:/bin"])}
    command = subprocess.Popen(
        [sys.executable, "-m", "sil.cli", "run", "run.json", "--diagnostics",
         "-o", "run.mcap"],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_observer(observer)

    command.send_signal(termination_signal)
    stdout, stderr = command.communicate(timeout=20)

    assert "run interrupted" in stderr
    assert "run: interrupted, exit code 1, partial (interrupted)" in stdout
    assert_cleanup(command, observer, regions, expected_code=1, stderr=stderr)
    report = json.loads((tmp_path / "run.mcap.diagnostics.json").read_text())
    assert report["run"]["status"] == "interrupted"


def test_manifest_relative_inputs_keep_their_meaning(native_path, tmp_path):
    """A Manifest elsewhere resolves its inputs against its own directory."""
    project = tmp_path / "my project"
    (project / "participants").mkdir(parents=True)
    script = project / "participants" / "echo.py"
    script.write_text((ROOT / "tests" / "participants" / "echo.py").read_text())
    m = toy_manifest(duration_ns=20 * MS)
    m.add_channel("echo", schema="toy.Counter")
    m.add_process("pyecho", command=[sys.executable, "participants/echo.py"],
                  step_period_ns=10 * MS, publishes=["echo"])
    manifest = m.write(project / "manifest.json").path
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = sil(["run", "../my project/manifest.json", "--diagnostics",
                "-o", "out dir.mcap"], native_path, elsewhere)

    assert proc.returncode == 0, proc.stderr
    report = json.loads((elsewhere / "out dir.mcap.diagnostics.json")
                        .read_text())
    assert report["identity"]["manifest_path"] == "../my project/manifest.json"
    assert [row["participant"] for row in
            report["execution"]["participants"]] == ["pyecho"]
    assert manifest.is_file()


def test_ordinary_run_without_diagnostics_still_replaces_sil(tmp_path):
    runner = tmp_path / "sil-run"
    runner.write_text('#!/bin/sh\necho "runner got: $*"\nexit 4\n')
    runner.chmod(0o755)

    proc = sil(["run", "m.json", "-o", "x.mcap", "--diagnostics-outx"],
               tmp_path, tmp_path)

    assert proc.returncode == 4
    assert proc.stdout == "runner got: m.json -o x.mcap --diagnostics-outx\n"


def test_manifest_relative_replay_is_summarized_apart_from_tasks(
    native_path, tmp_path, monkeypatch
):
    project = tmp_path / "replay project"
    project.mkdir()
    source = toy_manifest(duration_ns=50 * MS)
    source.add_channel("ticks", schema="toy.Counter")
    add_producer(source, "aprod", channel="ticks", period_ns=10 * MS)
    source.write(project / "src.json")
    recorded = sil(["run", "src.json", "-o", "record.mcap"], native_path,
                   project)
    assert recorded.returncode == 0, recorded.stderr
    m = toy_manifest(duration_ns=50 * MS)
    m.add_channel("ticks", schema="toy.Counter")
    monkeypatch.chdir(project)  # the builder hashes the Recording it names
    m.add_replay("rep", recording="record.mcap", channels=["ticks"])
    m.write(project / "replay.json")

    proc = sil(["run", "replay project/replay.json", "--diagnostics",
                "--no-recording", "--diagnostics-out", "replay.json"],
               native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert any(line.startswith("  rep replay: 5 Messages; publication Slots "
                               "5 of 5 completed, mean ")
               for line in proc.stdout.splitlines())


# --- --html (#304) -------------------------------------------------------------

FAKE_SUCCESS = ('for a; do last="$a"; done\necho manifest_hash fake\n'
                f'cp "{FIXTURES / "run-success.json"}" "$last"\nexit 0\n')


def test_html_adds_a_page_of_the_kept_json(native_path, tmp_path):
    from sil.diagnostics_html import render

    proc = sil(["run", str(mixed_period_manifest(tmp_path)), "-o", "run.mcap",
                "--html", "run.html"], native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    report = (tmp_path / "run.mcap.diagnostics.json").read_text()
    assert (tmp_path / "run.html").read_text() == render(report)
    assert proc.stdout.endswith("HTML report: run.html\n")
    assert "diagnostics report: run.mcap.diagnostics.json" in proc.stdout


def test_html_of_a_failed_run_keeps_the_run_exit_code(native_path, tmp_path):
    proc = sil(["run", str(failing_step_manifest(tmp_path)), "-o", "run.mcap",
                "--html", "run.html"], native_path, tmp_path)

    assert proc.returncode == 1
    assert "Run failure" in (tmp_path / "run.html").read_text()
    assert "HTML report: run.html" in proc.stdout


@pytest.mark.parametrize("argv, message", [
    (["--html", "taken.html"], "HTML report 'taken.html' already exists"),
    (["--html", "missing/run.html"], "does not exist"),
    (["--diagnostics-out", "r.json", "--html", "r.json"],
     "HTML report 'r.json' is also the diagnostics report"),
    (["-o", "run.mcap", "--html", "run.mcap"], "is also the Recording"),
])
def test_rejected_html_destinations_start_no_run(build_dir, tmp_path, argv,
                                                 message):
    counter = tmp_path / "started"
    path = fake_tools(tmp_path, build_dir, f"touch '{counter}'\nexit 0\n")
    (tmp_path / "taken.html").write_text("keep")

    proc = sil(["run", "m.json", *argv], path, tmp_path)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert not counter.exists()
    assert (tmp_path / "taken.html").read_text() == "keep"


def test_no_report_means_no_html_and_exit_1(build_dir, tmp_path):
    path = fake_tools(tmp_path, build_dir, "exit 0\n")

    proc = sil(["run", "m.json", "--html", "run.html"], path, tmp_path)

    assert proc.returncode == 1
    assert ("sil run: cannot write the HTML report 'run.html': no valid "
            "diagnostics report to render") in proc.stderr
    assert not (tmp_path / "run.html").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_an_unwritable_html_report_exits_1_and_keeps_the_json(build_dir,
                                                              tmp_path):
    path = fake_tools(tmp_path, build_dir, FAKE_SUCCESS)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        proc = sil(["run", "m.json", "--html", "locked/run.html"], path,
                   tmp_path)
    finally:
        locked.chmod(0o755)

    assert proc.returncode == 1
    assert "sil run: cannot write the HTML report 'locked/run.html': " \
        in proc.stderr
    assert json.loads((tmp_path / "out.mcap.diagnostics.json").read_text())[
        "run"]["exit_code"] == 0
    assert list(locked.iterdir()) == []


def test_an_html_report_that_appears_during_the_run_is_never_replaced(
    build_dir, tmp_path
):
    """Preflight found no HTML file; one created during the Run keeps its
    content, the command exits 1, and no temporary file is left beside it."""
    pages = tmp_path / "pages"
    pages.mkdir()
    page = pages / "run.html"
    path = fake_tools(tmp_path, build_dir,
                      f"printf earlier > '{page}'\n" + FAKE_SUCCESS)

    proc = sil(["run", "m.json", "--html", "pages/run.html"], path, tmp_path)

    assert proc.returncode == 1
    assert "sil run: cannot write the HTML report 'pages/run.html': " \
        in proc.stderr
    assert page.read_text() == "earlier"
    assert sorted(p.name for p in pages.iterdir()) == ["run.html"]
