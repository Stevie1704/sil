"""`sil profile`: one CPU capture of one fresh Run (#315).

The command runs as its own process with a PATH that holds the built native
binaries and tests/fake_perf.py as `perf`, so these tests see what a user
sees: exit code, stdout, stderr and the profile directory. The fake has the
process behavior of perf and samples nothing; the real backend is
tests/test_profile_perf_integration.py.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import zipfile
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from sil.profile_report import validate_profile_report
from test_process_descendants import (
    _wait_for_observer, assert_cleanup, descendant_manifest,
)
from test_profile_cpu import SCRIPT, SRCLINES
from test_profile_modules import BUILD_ID, elf
from test_run_diagnostics import (
    MS, failing_step_manifest, mixed_period_manifest,
)
from toys import toy_manifest

NATIVE = ("sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher")
FAKE_PERF = ROOT / "tests" / "fake_perf.py"


@pytest.fixture
def tools(build_dir, tmp_path) -> Path:
    """A PATH directory with the built native binaries and a fake perf."""
    directory = tmp_path / "tools"
    directory.mkdir()
    for name in NATIVE:
        (directory / name).symlink_to(build_dir / name)
    perf = directory / "perf"
    perf.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_PERF} \"$@\"\n")
    perf.chmod(0o755)
    return directory


@pytest.fixture
def exports(tmp_path) -> dict[str, str]:
    """The canned perf exports: the synthetic capture of test_profile_cpu."""
    script = tmp_path / "script.txt"
    # Without its lost record: a complete capture.
    script.write_text("\n".join(line for line in SCRIPT.splitlines()
                                if "PERF_RECORD_LOST" not in line) + "\n")
    srclines = tmp_path / "srclines.txt"
    srclines.write_text(SRCLINES)
    return {"FAKE_PERF_SCRIPT": str(script),
            "FAKE_PERF_SRCLINES": str(srclines),
            "FAKE_PERF_LOG": str(tmp_path / "perf-log.jsonl")}


def sil(argv, tools: Path, cwd: Path, env: dict | None = None,
        wait: bool = True, system_path: str = "/usr/bin:/bin", **kwargs):
    environment = {**os.environ, **(env or {}),
                   "PATH": os.pathsep.join([str(tools), system_path])}
    command = [sys.executable, "-m", "sil.cli", "profile", *argv]
    if not wait:
        return subprocess.Popen(command, cwd=cwd, env=environment,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    return subprocess.run(command, capture_output=True, text=True, cwd=cwd,
                          env=environment, **kwargs)


def profile(directory: Path) -> dict:
    report = json.loads((directory / "profile.json").read_text())
    assert validate_profile_report(report) == []
    return report


def perf_log(exports) -> list[dict]:
    path = Path(exports["FAKE_PERF_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_successful_capture_writes_the_profile_directory(
        tools, exports, tmp_path, sil_run):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports)

    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "prof"
    report = profile(out)
    assert report["run"]["status"] == "succeeded"
    capture = report["capture"]
    assert (capture["kind"], capture["status"]) == ("cpu", "complete")
    assert capture["totals"]["samples"] == 7
    assert capture["sampling"]["event"] == "cpu-clock:u"
    assert report["activation_correlation"] == {"kind": "not_requested"}
    assert sorted(p.name for p in out.iterdir()) == [
        "cpu", "profile.json", "run.diagnostics.json", "run.mcap",
        "run.mcap.provenance.json"]
    assert sorted(p.name for p in (out / "cpu").iterdir()) == [
        "buildid-cache", "perf-buildid-cache.stderr.txt",
        "perf-script.stderr.txt", "perf-srcline.stderr.txt",
        "perf.data", "samples.json"]
    assert not list(tmp_path.glob(".sil-run-*"))
    samples = json.loads((out / "cpu" / "samples.json").read_text())
    assert len(samples["samples"]) == 7
    assert "profile: prof/profile.json" in proc.stdout
    assert "capture: cpu, complete" in proc.stdout
    # The profiled Run records what the ordinary runner records.
    ordinary = subprocess.run([str(sil_run), str(manifest), "-o",
                               str(tmp_path / "ordinary.mcap")],
                              capture_output=True, cwd=tmp_path)
    assert ordinary.returncode == 0
    assert (out / "run.mcap").read_bytes() == \
        (tmp_path / "ordinary.mcap").read_bytes()


def test_a_copied_portable_set_renders_without_the_tools(tools, exports,
                                                         tmp_path):
    """#318: profile.json and cpu/samples.json alone render offline, with
    neither perf, the runner nor any profiled binary on PATH."""
    manifest = mixed_period_manifest(tmp_path)
    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports)
    assert proc.returncode == 0, proc.stderr
    copy = tmp_path / "elsewhere"
    copy.mkdir()
    for relative in ("profile.json", "cpu/samples.json"):
        (copy / relative).parent.mkdir(parents=True, exist_ok=True)
        (copy / relative).write_bytes((tmp_path / "prof" / relative)
                                      .read_bytes())

    rendered = subprocess.run(
        [sys.executable, "-m", "sil.cli", "profile", "render",
         "profile.json", "--html", "report.html"],
        capture_output=True, text=True, cwd=copy,
        env={**os.environ, "PATH": "/nonexistent"})

    assert rendered.returncode == 0, rendered.stderr
    assert "capture: cpu, complete" in rendered.stdout
    html = (copy / "report.html").read_text()
    assert "Capture complete" in html
    assert 'id="profile-data"' in html  # the filters have their data


def test_the_run_keeps_its_command_and_environment(tools, exports,
                                                   tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu",
                "--participant-timeout-ms", "60000"], tools, tmp_path,
               exports)

    assert proc.returncode == 0, proc.stderr
    record = perf_log(exports)[0]
    arguments = record["record"]
    assert arguments[:arguments.index("-o")] == [
        "-e", "cpu-clock:u", "-F", "999", "-k", "monotonic",
        "--call-graph", "dwarf,8192", "--buildid-mmap", "-N", "-q", "--control",
        f"fifo:{os.path.realpath(tmp_path)}/prof/cpu/.control"]
    workload = arguments[arguments.index("--") + 1:]
    assert Path(workload[0]).name == "sil-diagnostics-launcher"
    assert workload[1] == "--status-fd" and workload[3] == "--environment-fd"
    assert Path(workload[5]).name == "sil-run-diagnostics"
    assert workload[6:8] == [str(manifest), "-o"]
    assert "--participant-timeout-ms" in workload
    assert "--retain-run-directory" in workload
    assert "PERF_BUILDID_DIR" not in record["env"]
    report = profile(tmp_path / "prof")
    assert report["configuration"]["participant_timeout_ms"] == 60000


ENV_MARKER = ROOT / "tests" / "participants" / "env_marker.py"


def test_participants_get_the_environment_of_an_ordinary_run(
        tools, exports, tmp_path, sil_run):
    # perf changes the environment of the command it starts (fake_perf.py
    # does as perf 6.x does); the Participants must not see that.
    m = toy_manifest(duration_ns=20 * MS)
    m.add_process("marker", command=[sys.executable, str(ENV_MARKER),
                                     str(tmp_path / "marker.json")],
                  step_period_ns=10 * MS)
    manifest = m.write(tmp_path / "marker.json.manifest").path
    env = {**os.environ, **exports, "TMPDIR": str(tmp_path),
           "PATH": os.pathsep.join([str(tools), "/usr/bin:/bin"])}
    ordinary = subprocess.run([str(sil_run), str(manifest), "-o", "o.mcap"],
                              capture_output=True, text=True, cwd=tmp_path,
                              env=env)
    assert ordinary.returncode == 0, ordinary.stderr
    expected = json.loads((tmp_path / "marker.json").read_text())

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, {**exports, "TMPDIR": str(tmp_path)})

    assert proc.returncode == 0, proc.stderr
    profiled = json.loads((tmp_path / "marker.json").read_text())
    assert profiled["environment"] == expected["environment"]


@pytest.mark.parametrize("argv, message", [
    (["--collector", "gpu"], "unknown collector 'gpu'; use cpu or heap"),
    (["--collector", "cpu", "--runner"], "--runner selects a heap scope"),
    (["--collector", "cpu", "--participant", "x"],
     "--participant selects a heap scope"),
    (["--collector", "cpu", "-o", "a.mcap", "--no-recording"],
     "-o and --no-recording exclude each other"),
])
def test_invalid_options_are_usage_errors(tools, exports, tmp_path, argv,
                                          message):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", *argv], tools, tmp_path,
               exports)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert not (tmp_path / "prof").exists()


def test_an_existing_output_directory_is_never_replaced(tools, exports,
                                                        tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    (tmp_path / "prof").mkdir()
    (tmp_path / "prof" / "keep.txt").write_text("earlier evidence")

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports)

    assert proc.returncode == 2
    assert "'prof' already exists" in proc.stderr
    assert [p.name for p in (tmp_path / "prof").iterdir()] == ["keep.txt"]
    assert not Path(exports["FAKE_PERF_LOG"]).exists()


def test_a_recording_that_is_the_manifest_is_a_usage_error(tools, exports,
                                                           tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu", "-o",
                str(manifest)], tools, tmp_path, exports)

    assert proc.returncode == 2
    assert "is the Manifest" in proc.stderr


def test_missing_perf_is_an_actionable_preflight_error(tools, exports,
                                                       tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    (tools / "perf").unlink()

    # Only the tools directory: a host can have perf in /usr/bin.
    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports, system_path=str(tmp_path / "empty"))

    assert proc.returncode == 1
    assert "cannot find perf on PATH" in proc.stderr
    assert not (tmp_path / "prof").exists()


def test_a_perf_permission_failure_stops_before_any_participant(
        tools, exports, tmp_path):
    observer = tmp_path / "observer.json"
    manifest = descendant_manifest("descendant-success", observer).write(
        tmp_path / "run.json").path

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, {**exports, "FAKE_PERF_DENY": "1"})

    assert proc.returncode == 1
    assert "perf cannot sample cpu-clock:u" in proc.stderr
    assert "perf_event_paranoid" in proc.stderr
    assert "CAP_PERFMON" in proc.stderr
    assert not observer.exists()
    assert not (tmp_path / "prof").exists()


def test_a_missing_runner_is_not_found(tools, exports, tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    (tools / "sil-run-diagnostics").unlink()

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports)

    assert proc.returncode == 127
    assert "sil-run-diagnostics" in proc.stderr


def test_a_launcher_failure_is_reported_on_stderr(tools, exports, tmp_path):
    # A runner that the launcher cannot execute: no Run takes place.
    manifest = mixed_period_manifest(tmp_path)
    runner = tools / "sil-run-diagnostics"
    runner.unlink()
    runner.write_text("#!/nonexistent/interpreter\n")
    runner.chmod(0o755)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports)

    assert proc.returncode == 127
    assert "no Run took place" in proc.stderr
    assert "cannot run" in proc.stderr
    assert profile(tmp_path / "prof")["run"]["status"] == "not_started"


def test_a_failed_run_keeps_its_exit_code_and_first_diagnostic(
        tools, exports, tmp_path):
    manifest = failing_step_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports)

    assert proc.returncode == 1
    assert "boom" in proc.stderr.splitlines()[0]
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "run_failure"
    assert report["run"]["exit_code"] == 1
    assert "boom" in report["run"]["failure"]["message"]
    assert report["capture"]["status"] == "complete"


def test_a_configuration_error_keeps_exit_code_2_with_a_profile(
        tools, exports, tmp_path):
    m = toy_manifest(duration_ns=10 * MS)
    m.add_process("missing", command=[str(tmp_path / "no-such-program")],
                  step_period_ns=10 * MS)
    manifest = m.write(tmp_path / "bad.json").path

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, exports)

    assert proc.returncode == 2
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "configuration_error"


def test_a_perf_failure_after_a_successful_run_exits_1(tools, exports,
                                                       tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, {**exports, "FAKE_PERF_RECORD_EXIT": "255"})

    assert proc.returncode == 1
    report = profile(tmp_path / "prof")
    assert report["run"] == {**report["run"], "status": "succeeded",
                             "exit_code": 0}
    assert report["capture"]["status"] == "failed"
    assert report["capture"]["reasons"] == ["backend_failed"]
    assert "capture: cpu, failed (backend_failed)" in proc.stdout


def test_an_export_failure_keeps_the_raw_capture(tools, exports, tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, {**exports, "FAKE_PERF_SCRIPT_FAIL": "1"})

    assert proc.returncode == 1
    report = profile(tmp_path / "prof")
    assert report["capture"]["reasons"] == ["export_failed"]
    assert report["capture"]["samples_file"] is None
    assert (tmp_path / "prof" / "cpu" / "perf.data").exists()
    assert "failed to process sample" in (
        tmp_path / "prof" / "cpu" / "perf-script.stderr.txt").read_text()


def test_an_export_above_its_limit_fails_the_capture_not_the_run(
        tools, exports, tmp_path):
    # docs/profile-report.md: each perf script pass may write at most twice
    # --max-capture-bytes. The canned export is larger than 2 x 200 bytes;
    # the fake perf.data is smaller than 200 bytes.
    assert Path(exports["FAKE_PERF_SCRIPT"]).stat().st_size > 400
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu",
                "--max-capture-bytes", "200"], tools, tmp_path, exports)

    assert proc.returncode == 1
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "succeeded"
    assert report["capture"]["status"] == "failed"
    assert report["capture"]["reasons"] == ["export_failed"]
    assert report["capture"]["samples_file"] is None
    assert (tmp_path / "prof" / "cpu" / "perf.data").exists()
    assert "export limit of 400 bytes" in (
        tmp_path / "prof" / "cpu" / "perf-script.stderr.txt").read_text()


def test_lost_samples_make_a_partial_capture(tools, exports, tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    stats = tmp_path / "stats.txt"
    stats.write_text("Aggregated stats:\n  LOST_SAMPLES events:          1\n")

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, {**exports, "FAKE_PERF_STATS": str(stats)})

    assert proc.returncode == 1
    capture = profile(tmp_path / "prof")["capture"]
    assert capture["status"] == "partial"
    assert capture["reasons"] == ["lost_samples"]
    assert capture["totals"]["lost_samples"] is None
    assert capture["totals"]["lost_records"] == 1


def test_the_capture_limit_stops_sampling_not_the_run(tools, exports,
                                                      tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu",
                "--max-capture-bytes", "100000"], tools, tmp_path,
               {**exports, "FAKE_PERF_GROW": "1"})

    assert proc.returncode == 1
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "succeeded"
    capture = report["capture"]
    assert capture["status"] == "partial"
    assert "capture_limit_reached" in capture["reasons"]
    assert capture["limits"]["limit_reached"] is True
    data = (tmp_path / "prof" / "cpu" / "perf.data").read_bytes()
    assert data.endswith(b'{"disabled": true}')


def test_a_symbolization_timeout_degrades_source_lines_only(
        tools, exports, tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu",
                "--symbolization-timeout-s", "1"], tools, tmp_path,
               {**exports, "FAKE_PERF_SRCLINE_SLEEP": "5"})

    assert proc.returncode == 0, proc.stderr
    capture = profile(tmp_path / "prof")["capture"]
    assert capture["status"] == "complete"
    assert capture["symbolization"]["source_lines"]["unavailable"] == {
        "samples_with_line": "symbolization_timeout"}


def writer_manifest(tmp_path: Path, library: Path, *, remove: bool) -> Path:
    """A Process participant that copies a library into its working
    directory and, as the FMI importer does at shutdown, can remove it."""
    shutdown = "Path('fmu.so').unlink()\n" if remove else ""
    m = toy_manifest(duration_ns=10 * MS)
    m.add_process("writer", command=[
        sys.executable, "-c",
        "import json, shutil, sys\nfrom pathlib import Path\n"
        f"shutil.copy({str(library)!r}, 'fmu.so')\n"
        "for line in sys.stdin:\n"
        "    op = json.loads(line)['op']\n"
        "    if op == 'init': print(json.dumps({'op': 'ready'}), flush=True)\n"
        "    elif op == 'step': print(json.dumps({'op': 'step_done', "
        "'out': []}), flush=True)\n"
        "    else: break\n" + shutdown], step_period_ns=10 * MS)
    return m.write(tmp_path / "writer.json").path


def test_a_removed_module_is_captured_from_a_named_archive(
        tools, exports, tmp_path):
    library = elf(tmp_path / "built" / "fmu.so", build_id=BUILD_ID,
                  debug_lines=True)
    archive = tmp_path / "Model.fmu"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.write(library, "binaries/x86_64-linux/fmu.so")
    manifest = writer_manifest(tmp_path, library, remove=True)
    removed = f"{os.path.realpath(tmp_path)}/.sil-run-x/p/fmu.so"
    buildids = tmp_path / "buildids.txt"
    buildids.write_text(f"{BUILD_ID} {removed}\n")
    script = tmp_path / "removed-script.txt"
    script.write_text(
        "          python3   200/200   10.000100000:    1001001 \n"
        f"\t    7f0000000100 fmu_spin ({removed})\n")
    srclines = tmp_path / "removed-srclines.txt"
    srclines.write_text("    7f0000000100 fmu_spin\n  fmu.c:3\n")
    env = {**exports, "FAKE_PERF_BUILDIDS": str(buildids),
           "FAKE_PERF_SCRIPT": str(script),
           "FAKE_PERF_SRCLINES": str(srclines)}

    without = sil([str(manifest), "--out", "first", "--collector", "cpu"],
                  tools, tmp_path, env)
    named = sil([str(manifest), "--out", "second", "--collector", "cpu",
                 "--modules-from", str(archive)], tools, tmp_path, env)

    assert without.returncode == 0, without.stderr
    module, = profile(tmp_path / "first")["capture"]["modules"]
    assert module["material"] == "unavailable"
    assert "--modules-from" in without.stdout
    assert named.returncode == 0, named.stderr
    module, = profile(tmp_path / "second")["capture"]["modules"]
    assert module["material"] == "captured_modules_from"
    assert (tmp_path / "second" / "cpu" / "buildid-cache" / "fmu.so").exists()
    assert not (tmp_path / "second" / "cpu" / ".modules-from").exists()
    failed = sil([str(manifest), "--out", "third", "--collector", "cpu",
                  "--modules-from", str(archive)], tools, tmp_path,
                 {**env, "FAKE_PERF_CACHE_FAIL": "1"})
    assert failed.returncode == 0, failed.stderr
    module, = profile(tmp_path / "third")["capture"]["modules"]
    assert module["material"] == "unavailable"
    assert "Couldn't add" in (tmp_path / "third" / "cpu" /
                              "perf-buildid-cache.stderr.txt").read_text()


def test_a_missing_modules_from_source_is_a_usage_error(tools, exports,
                                                        tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu",
                "--modules-from", "absent.fmu"], tools, tmp_path, exports)

    assert proc.returncode == 2
    assert "--modules-from 'absent.fmu' does not exist" in proc.stderr


def test_a_module_from_the_run_directory_is_retained_for_symbolization(
        tools, exports, tmp_path):
    """A participant that leaves a library in its working directory: the
    runner retains the tree, so the library is captured."""
    library = elf(tmp_path / "built" / "fmu.so", build_id=BUILD_ID,
                  debug_lines=True)
    manifest = writer_manifest(tmp_path, library, remove=False)
    retained_name = "{RETAINED:fmu.so}"
    buildids = tmp_path / "buildids.txt"
    buildids.write_text(f"{BUILD_ID} {retained_name}\n")
    script = tmp_path / "fmu-script.txt"
    script.write_text(
        "          python3   200/200   10.000000000: PERF_RECORD_MMAP2 "
        f"200/200: [0x7f0000000000(0x1000) @ 0x1000 <{BUILD_ID}>]: r-xp "
        f"{retained_name}\n"
        "          python3   200/200   10.000100000:    1001001 \n"
        f"\t    7f0000000100 fmu_spin ({retained_name})\n")
    srclines = tmp_path / "fmu-srclines.txt"
    srclines.write_text("    7f0000000100 fmu_spin\n  fmu.c:3\n")

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu"], tools,
               tmp_path, {**exports, "FAKE_PERF_BUILDIDS": str(buildids),
                          "FAKE_PERF_SCRIPT": str(script),
                          "FAKE_PERF_SRCLINES": str(srclines)})

    assert proc.returncode == 0, proc.stderr
    report = profile(tmp_path / "prof")
    module, = report["capture"]["modules"]
    assert module["material"] == "captured_run_directory"
    assert module["debug"]["status"] == "embedded"
    assert (tmp_path / "prof" / "cpu" / "buildid-cache" / "fmu.so").exists()
    assert not (tmp_path / "prof" / "cpu" / ".run-directory").exists()
    assert report["capture"]["hotspots"]["self"][0]["source_lines"] == [
        {"file": "fmu.c", "line": 3, "samples": 1}]


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGHUP])
def test_a_signal_interrupts_the_run_and_the_capture(
        tools, exports, tmp_path, termination_signal):
    regions = tmp_path / "regions"
    regions.mkdir()
    observer = tmp_path / "observer.json"
    descendant_manifest("descendant-timeout", observer).write(
        tmp_path / "run.json")
    command = sil(["run.json", "--out", "prof", "--collector", "cpu"], tools,
                  tmp_path, {**exports, "TMPDIR": str(regions)}, wait=False)
    _wait_for_observer(observer)

    command.send_signal(termination_signal)
    stdout, stderr = command.communicate(timeout=30)

    assert_cleanup(command, observer, regions, expected_code=1,
                   stderr=stderr)
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "interrupted"
    assert report["run"]["supervisor_signal"] == termination_signal
    name = signal.Signals(termination_signal).name
    assert f"run interrupted by {name}" in report["run"]["failure"]["message"]
    assert report["capture"]["status"] == "interrupted"
    assert "capture: cpu, interrupted (interrupted)" in stdout


# --- Activation correlation (#316) -------------------------------------------


def test_activations_stream_observations_beside_the_capture(
        tools, exports, tmp_path, sil_run):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu",
                "--activations", "--max-activation-bytes", "100000"], tools,
               tmp_path, exports)

    out = tmp_path / "prof"
    report = profile(out)
    assert report["run"]["status"] == "succeeded"
    record = perf_log(exports)[0]["record"]
    assert record[record.index("--activation-observations") + 1] == str(
        out / "activation" / "observations.jsonl")
    assert record[record.index("--activation-observations-limit-bytes")
                  + 1] == "100000"
    lines = (out / "activation" / "observations.jsonl").read_text() \
        .splitlines()
    assert json.loads(lines[0])["limit_bytes"] == 100000
    assert json.loads(lines[-1])["kind"] == "end"
    roles = {a["role"]: a["path"] for a in report["artifacts"]}
    assert roles["activation_observations"] == "activation/observations.jsonl"
    correlation = report["activation_correlation"]
    assert correlation["kind"] == "activation"
    assert correlation["observations"] == "activation/observations.jsonl"
    # The fake perf saw no process of this Run, so the join is not proven:
    # it is never shown as correlated, and the command says so.
    assert correlation["status"] == "unavailable"
    assert correlation["reasons"][0] in ("runner_not_observed",
                                         "pid_namespace_unverified")
    assert correlation["summary"] is None
    assert proc.returncode == 1
    assert "activations: unavailable" in proc.stdout
    ordinary = subprocess.run([str(sil_run), str(manifest), "-o",
                               str(tmp_path / "ordinary.mcap")],
                              capture_output=True, cwd=tmp_path)
    assert (out / "run.mcap").read_bytes() == \
        (tmp_path / "ordinary.mcap").read_bytes()


def test_an_activation_limit_without_activations_is_a_usage_error(
        tools, exports, tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil([str(manifest), "--out", "prof", "--collector", "cpu",
                "--max-activation-bytes", "100000"], tools, tmp_path, exports)

    assert proc.returncode == 2
    assert "--max-activation-bytes needs --activations" in proc.stderr
    assert not (tmp_path / "prof").exists()
