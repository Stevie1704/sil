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
from pathlib import Path

import pytest

from conftest import ROOT
from sil.diagnostics_report import validate_run_report
from sil.diagnostics_run import format_summary
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
    (["--html", "run.html"], "--html is not available in this version"),
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
    # The runner's first diagnostic is the first stderr line, unchanged.
    assert proc.stderr.startswith("sil-run: participant 'test' failed")
    assert "run: run_failure, exit code 1, partial (run_failure)" in proc.stdout
    assert "manifest_hash" not in proc.stdout


def test_manifest_error_exits_2_with_a_preflight_report(native_path, tmp_path):
    (tmp_path / "broken.json").write_text("{}")

    proc = sil(["run", "broken.json", "--diagnostics", "-o", "run.mcap"],
               native_path, tmp_path)

    assert proc.returncode == 2
    assert "run: configuration_error, exit code 2" in proc.stdout
    assert proc.stderr.startswith("sil-run: ")


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
    launcher = ("echo 'sil-diagnostics-launcher: fork failed: Resource "
                "temporarily unavailable' >&2\nexit 1\n")
    path = fake_tools(tmp_path, build_dir, "exit 0\n", launcher)

    proc = sil(["run", "m.json", "--diagnostics"], path, tmp_path)

    assert proc.returncode == 1
    assert proc.stderr.splitlines() == [
        "sil-diagnostics-launcher: fork failed: Resource temporarily "
        "unavailable",
        "sil run: sil-diagnostics-launcher failed, so no Run took place: "
        "fork failed: Resource temporarily unavailable",
    ]


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
