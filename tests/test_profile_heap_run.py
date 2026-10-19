"""`sil profile --collector heap`: one Heaptrack capture of one scope (#317).

The command runs as its own process, as in tests/test_profile_run.py. The
usage and scope checks run on every host. The capture tests need Linux: the
fake Heaptrack (tests/fake_heaptrack/) is a preload library with the process
behavior of Heaptrack's, so the launch adapter, the FIFO, the interpreter
and the scope are the real ones. The real backend is
tests/test_profile_heap_integration.py.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from sil.manifest import Manifest
from sil.profile_report import validate_profile_report

from conftest import ROOT
from test_process_descendants import (
    _wait_for_observer, assert_cleanup, descendant_manifest,
)
from test_profile_heap import DATA
from test_run_diagnostics import MS, mixed_period_manifest

NATIVE = ("sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher")
FAKE = ROOT / "tests" / "fake_heaptrack"
LINUX = platform.system() == "Linux"
needs_linux = pytest.mark.skipif(not LINUX, reason="Heaptrack is Linux only")


@pytest.fixture
def tools(build_dir, tmp_path) -> Path:
    directory = tmp_path / "tools"
    directory.mkdir()
    for name in NATIVE:
        (directory / name).symlink_to(build_dir / name)
    return directory


def sil(argv, tools: Path, cwd: Path, env: dict | None = None,
        wait: bool = True, **kwargs):
    environment = {key: value for key, value in os.environ.items()
                   if key != "LD_PRELOAD"}
    environment.update(env or {})
    environment["PATH"] = os.pathsep.join([str(tools), "/usr/bin:/bin"])
    command = [sys.executable, "-m", "sil.cli", "profile", *argv]
    if not wait:
        return subprocess.Popen(command, cwd=cwd, env=environment,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    return subprocess.run(command, capture_output=True, text=True, cwd=cwd,
                          env=environment, **kwargs)


def heap(manifest, *scope, out="prof"):
    return [str(manifest), "--out", out, "--collector", "heap", *scope]


def shimmed_manifest(tmp_path: Path) -> Path:
    m = Manifest(duration_ns=20 * MS)
    m.add_process("clocked", command=[
        sys.executable, str(ROOT / "tests" / "participants" / "echo.py")],
        step_period_ns=10 * MS, shim=True, sleep="immediate")
    return m.write(tmp_path / "shimmed.json").path


# --- usage and scope: exit 2 before anything starts --------------------------


@pytest.mark.parametrize("scope, message", [
    ((), "--collector heap needs one scope: --runner or --participant NAME"),
    (("--runner", "--participant", "pyecho"),
     "--runner and --participant exclude each other"),
    (("--runner", "--frequency-hz", "99"),
     "--frequency-hz is an option of the cpu collector"),
    (("--runner", "--activations"),
     "Activation correlation is unavailable for the heap collector"),
    (("--runner", "--modules-from", "x.fmu"),
     "--modules-from is an option of the cpu collector"),
])
def test_an_invalid_heap_request_is_a_usage_error(tools, tmp_path, scope,
                                                  message):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(heap(manifest, *scope), tools, tmp_path)

    assert proc.returncode == 2, proc.stderr
    assert message in proc.stderr
    assert not (tmp_path / "prof").exists()


@pytest.mark.parametrize("participant, message", [
    ("multi", "'multi' is a Native participant: it runs inside the runner "
              "process, so it cannot be a heap scope; use --runner"),
    ("nobody", "the Manifest has no participant 'nobody'"),
])
def test_a_participant_that_is_no_process_is_rejected(tools, tmp_path,
                                                      participant, message):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(heap(manifest, "--participant", participant), tools, tmp_path)

    assert proc.returncode == 2, proc.stderr
    assert message in proc.stderr
    assert not (tmp_path / "prof").exists()
    assert not list(tmp_path.glob(".sil-run-*"))


def test_a_shimmed_process_participant_is_rejected(tools, tmp_path):
    manifest = shimmed_manifest(tmp_path)

    proc = sil(heap(manifest, "--participant", "clocked"), tools, tmp_path)

    assert proc.returncode == 2, proc.stderr
    assert "'clocked' uses the Clock shim" in proc.stderr
    assert "issues/330" in proc.stderr
    assert not (tmp_path / "prof").exists()


def test_an_existing_profile_directory_is_never_replaced(tools, tmp_path):
    manifest = mixed_period_manifest(tmp_path)
    (tmp_path / "prof").mkdir()

    proc = sil(heap(manifest, "--runner"), tools, tmp_path)

    assert proc.returncode == 2
    assert "already exists" in proc.stderr
    assert list((tmp_path / "prof").iterdir()) == []


@pytest.mark.skipif(shutil.which("heaptrack", path="/usr/bin:/bin")
                    is not None, reason="Heaptrack is installed here")
def test_a_host_without_heaptrack_stops_in_preflight(tools, tmp_path):
    manifest = mixed_period_manifest(tmp_path)

    proc = sil(heap(manifest, "--runner"), tools, tmp_path)

    assert proc.returncode == 1, proc.stderr
    assert "heaptrack" in proc.stderr.lower()
    assert not (tmp_path / "prof").exists()
    assert not list(tmp_path.glob(".sil-run-*"))


# --- capture with the fake Heaptrack (Linux) ---------------------------------

ENV_MARKER = ROOT / "tests" / "participants" / "env_marker.py"
PROBE = ROOT / "tests" / "participants" / "working_directory_probe.py"
# The canned data: what the fake preload library writes after its header.
BODY = "".join(DATA.splitlines(keepends=True)[2:])


@pytest.fixture
def heaptrack(tools, tmp_path) -> dict[str, str]:
    """The fake Heaptrack in `tools`, and the environment of its data."""
    shutil.copy2(FAKE / "heaptrack", tools / "heaptrack")
    library = tools / "lib" / "heaptrack"
    (library / "libexec").mkdir(parents=True)
    subprocess.run(["cc", "-shared", "-fPIC", "-o",
                    str(library / "libheaptrack_preload.so"),
                    str(FAKE / "fake_preload.c")], check=True)
    interpret = library / "libexec" / "heaptrack_interpret"
    interpret.write_text(f"#!/bin/sh\nexec {sys.executable} "
                         f"{FAKE / 'heaptrack_interpret'} \"$@\"\n")
    interpret.chmod(0o755)
    body = tmp_path / "heaptrack-body.txt"
    body.write_text(BODY)
    return {"FAKE_HEAPTRACK_DATA": str(body)}


def profile(directory: Path) -> dict:
    report = json.loads((directory / "profile.json").read_text())
    assert validate_profile_report(report) == []
    return report


def marker_manifest(tmp_path: Path, *, shim: bool = False) -> tuple:
    """Two Process participants that write their preload environment."""
    markers = tmp_path / "markers"
    markers.mkdir()
    m = Manifest(duration_ns=20 * MS)
    options = {"shim": True, "sleep": "immediate"} if shim else {}
    for name in ("scoped", "sibling"):
        m.add_process(name, command=[sys.executable, str(ENV_MARKER),
                                     str(markers / f"{name}.json")],
                      step_period_ns=10 * MS, **options)
    return m.write(tmp_path / "markers.json").path, markers


def marker(markers: Path, name: str) -> dict:
    return json.loads((markers / f"{name}.json").read_text())


def ordinary_recording(sil_run, manifest: Path, cwd: Path) -> bytes:
    done = subprocess.run([str(sil_run), str(manifest), "-o",
                           str(cwd / "ordinary.mcap")],
                          capture_output=True, text=True, cwd=cwd)
    assert done.returncode == 0, done.stderr
    return (cwd / "ordinary.mcap").read_bytes()


@needs_linux
def test_the_runner_scope_captures_the_runner_only(tools, heaptrack,
                                                   tmp_path, sil_run):
    manifest, markers = marker_manifest(tmp_path)

    proc = sil(heap(manifest, "--runner"), tools, tmp_path, heaptrack)

    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "prof"
    report = profile(out)
    capture = report["capture"]
    assert (capture["status"], capture["scope"]) == ("complete",
                                                     {"kind": "runner"})
    assert capture["backend"]["version"] == "heaptrack 1.5.0"
    assert capture["activation_correlation"]["status"] == "unavailable"
    analysis = capture["analysis"]
    assert "sil-run-diagnostics" in analysis["traced_executable"]
    assert analysis["totals"]["allocations"] == 8
    assert analysis["allocations_file"] == "heap/allocations.json"
    # Heaptrack removes its preload from the runner's environment, so no
    # Process participant is traced.
    for name in ("scoped", "sibling"):
        assert marker(markers, name)["ld_preload"] is None
        assert marker(markers, name)["dump_heaptrack_output"] is None
    assert sorted(p.name for p in (out / "heap").iterdir()) == [
        "allocations.json", "heaptrack.data", "interpret.stderr.txt",
        "launch-adapter.sh"]
    assert not list(tmp_path.glob(".sil-run-*"))
    assert "capture: heap, complete" in proc.stdout
    assert "activation correlation: unavailable" in proc.stdout
    assert (out / "run.mcap").read_bytes() == ordinary_recording(
        sil_run, manifest, tmp_path)


@needs_linux
def test_a_process_scope_traces_that_participant_only(tools, heaptrack,
                                                      tmp_path, sil_run):
    manifest, markers = marker_manifest(tmp_path)

    proc = sil(heap(manifest, "--participant", "scoped"), tools, tmp_path,
               heaptrack)

    assert proc.returncode == 0, proc.stderr
    report = profile(tmp_path / "prof")
    capture = report["capture"]
    assert capture["scope"] == {"kind": "process_participant",
                                "participant": "scoped"}
    traced = capture["analysis"]["traced_executable"]
    assert str(ENV_MARKER) in traced and "scoped.json" in traced
    # Heaptrack removed its variables before the participant's own code
    # ran, and the sibling never got them.
    for name in ("scoped", "sibling"):
        assert marker(markers, name)["ld_preload"] is None
        assert marker(markers, name)["dump_heaptrack_output"] is None
    assert (tmp_path / "prof" / "run.mcap").read_bytes() == \
        ordinary_recording(sil_run, manifest, tmp_path)


HEAPTRACK_VARIABLES = {"LD_PRELOAD", "DUMP_HEAPTRACK_OUTPUT"}


@needs_linux
@pytest.mark.parametrize("scope", [("--runner",),
                                   ("--participant", "scoped")])
def test_participants_get_the_environment_of_an_ordinary_run(
        tools, heaptrack, tmp_path, sil_run, scope):
    manifest, markers = marker_manifest(tmp_path)
    # The environment that sil() gives the profiling command.
    environment = {key: value for key, value in os.environ.items()
                   if key != "LD_PRELOAD"}
    environment["PATH"] = os.pathsep.join([str(tools), "/usr/bin:/bin"])
    done = subprocess.run([str(sil_run), str(manifest), "-o", "o.mcap"],
                          capture_output=True, text=True, cwd=tmp_path,
                          env={**environment, **heaptrack})
    assert done.returncode == 0, done.stderr
    expected = {name: marker(markers, name)["environment"]
                for name in ("scoped", "sibling")}

    proc = sil(heap(manifest, *scope), tools, tmp_path, heaptrack)

    assert proc.returncode == 0, proc.stderr
    for name in ("scoped", "sibling"):
        seen = {key: value for key, value in
                marker(markers, name)["environment"].items()
                if key not in HEAPTRACK_VARIABLES}
        assert seen == expected[name], name


def test_the_launch_adapter_keeps_the_working_directory_variable(
        tmp_path):
    # sh sets PWD when it starts; the adapter must give the command the
    # PWD of the profiling command, or none when it had none.
    from sil.profile_heap_run import Heaptrack, adapter_text
    tools = Heaptrack(Path("/x/heaptrack"), Path("/x/preload.so"),
                      Path("/x/interpret"), None)
    for pwd in (None, "/stale/directory"):
        environment = {key: value for key, value in os.environ.items()
                       if key != "PWD"}
        if pwd is not None:
            environment["PWD"] = pwd
        adapter = tmp_path / f"adapter-{pwd is None}.sh"
        adapter.write_text(adapter_text(tools, tmp_path / "fifo",
                                        environment))
        adapter.chmod(0o700)
        done = subprocess.run([str(adapter), "/usr/bin/env"],
                              capture_output=True, text=True,
                              env=environment, cwd=tmp_path)
        assert done.returncode == 0, done.stderr
        seen = dict(line.split("=", 1) for line in done.stdout.splitlines()
                    if "=" in line)
        assert seen.get("PWD") == pwd


@needs_linux
def test_the_clock_shim_of_a_runner_scope_participant_is_kept(
        tools, heaptrack, tmp_path, sil_run):
    manifest, markers = marker_manifest(tmp_path, shim=True)

    proc = sil(heap(manifest, "--runner"), tools, tmp_path, heaptrack)

    assert proc.returncode == 0, proc.stderr
    preload = marker(markers, "scoped")["ld_preload"]
    assert preload and "clock_shim" in preload
    assert "heaptrack" not in preload
    assert (tmp_path / "prof" / "run.mcap").read_bytes() == \
        ordinary_recording(sil_run, manifest, tmp_path)


@needs_linux
def test_a_scope_process_that_dies_early_gives_a_partial_capture(
        tools, heaptrack, tmp_path):
    observer = tmp_path / "observer"
    observer.mkdir()
    m = Manifest(duration_ns=20 * MS)
    m.add_process("victim", command=[sys.executable, str(PROBE),
                                     str(observer), "sigkill"],
                  step_period_ns=10 * MS)
    manifest = m.write(tmp_path / "victim.json").path

    proc = sil(heap(manifest, "--participant", "victim"), tools, tmp_path,
               heaptrack)

    # The Run's own failure stays the exit code.
    assert proc.returncode == 1, proc.stderr
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "run_failure"
    capture = report["capture"]
    assert (capture["status"], capture["reasons"]) == (
        "partial", ["data_may_be_incomplete"])
    # The process ended before Heaptrack wrote its data at exit.
    assert capture["analysis"]["totals"]["allocations"] == 0


@needs_linux
def test_an_interpreter_failure_after_a_successful_run_exits_1(
        tools, heaptrack, tmp_path):
    manifest, _ = marker_manifest(tmp_path)

    proc = sil(heap(manifest, "--runner"), tools, tmp_path,
               {**heaptrack, "FAKE_HEAPTRACK_INTERPRET_EXIT": "3"})

    assert proc.returncode == 1, proc.stderr
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "succeeded"
    capture = report["capture"]
    assert (capture["status"], capture["reasons"]) == ("failed",
                                                       ["backend_failed"])
    assert capture["backend"]["interpreter_exit_code"] == 3
    # The raw data stays for native analysis.
    assert capture["raw"] == "heap/heaptrack.data"


@needs_linux
def test_an_interpreter_that_does_not_end_is_stopped_at_its_timeout(
        tools, heaptrack, tmp_path):
    manifest, _ = marker_manifest(tmp_path)

    proc = sil(heap(manifest, "--runner", "--symbolization-timeout-s", "1"),
               tools, tmp_path,
               {**heaptrack, "FAKE_HEAPTRACK_INTERPRET_HANG": "1"},
               timeout=60)

    assert proc.returncode == 1, proc.stderr
    assert (tmp_path / "prof" / "profile.json").is_file(), proc.stderr
    capture = profile(tmp_path / "prof")["capture"]
    assert capture["status"] == "failed"
    assert "backend_failed" in capture["reasons"]
    assert capture["backend"]["interpreter_exit_code"] is None


@needs_linux
def test_the_capture_limit_keeps_the_run_and_marks_the_capture(
        tools, heaptrack, tmp_path):
    manifest, _ = marker_manifest(tmp_path)

    proc = sil(heap(manifest, "--runner", "--max-capture-bytes", "300"),
               tools, tmp_path, heaptrack)

    assert proc.returncode == 1, proc.stderr
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "succeeded"
    capture = report["capture"]
    assert capture["status"] == "partial"
    assert "capture_limit_reached" in capture["reasons"]
    assert capture["limits"]["limit_reached"] is True
    assert (tmp_path / "prof" / "heap" / "heaptrack.data").stat().st_size \
        == 300


@needs_linux
def test_undecodable_data_fails_the_capture_and_keeps_the_raw_file(
        tools, heaptrack, tmp_path):
    manifest, _ = marker_manifest(tmp_path)
    Path(heaptrack["FAKE_HEAPTRACK_DATA"]).write_text("t 9 9\na 10 1\n+ 0\n")

    proc = sil(heap(manifest, "--runner"), tools, tmp_path, heaptrack)

    assert proc.returncode == 1, proc.stderr
    capture = profile(tmp_path / "prof")["capture"]
    assert (capture["status"], capture["reasons"]) == ("failed",
                                                       ["export_failed"])
    assert capture["analysis"] is None
    assert capture["raw"] == "heap/heaptrack.data"
    assert not (tmp_path / "prof" / "heap" / "allocations.json").exists()


@needs_linux
@pytest.mark.parametrize("termination_signal", [signal.SIGINT,
                                                signal.SIGTERM])
def test_a_signal_interrupts_the_run_and_the_capture(
        tools, heaptrack, tmp_path, termination_signal):
    regions = tmp_path / "regions"
    regions.mkdir()
    observer = tmp_path / "observer.json"
    descendant_manifest("descendant-timeout", observer).write(
        tmp_path / "run.json")
    command = sil(heap("run.json", "--participant", "descendant"), tools,
                  tmp_path, {**heaptrack, "TMPDIR": str(regions)},
                  wait=False)
    _wait_for_observer(observer)

    command.send_signal(termination_signal)
    stdout, stderr = command.communicate(timeout=30)

    assert_cleanup(command, observer, regions, expected_code=1,
                   stderr=stderr)
    report = profile(tmp_path / "prof")
    assert report["run"]["status"] == "interrupted"
    assert report["capture"]["status"] == "interrupted"
    assert "interrupted" in report["capture"]["reasons"]
    assert not (tmp_path / "prof" / "heap" / ".fifo").exists()


@needs_linux
def test_a_preload_library_that_writes_nothing_stops_in_preflight(
        tools, heaptrack, tmp_path):
    manifest, markers = marker_manifest(tmp_path)
    (tools / "lib" / "heaptrack" / "libheaptrack_preload.so").write_text("")

    proc = sil(heap(manifest, "--runner"), tools, tmp_path, heaptrack)

    assert proc.returncode == 1, proc.stderr
    assert "wrote no data into a probe process" in proc.stderr
    assert not (tmp_path / "prof").exists()
    assert not list(markers.iterdir())


@needs_linux
def test_an_inherited_ld_preload_stops_in_preflight(tools, heaptrack,
                                                    tmp_path):
    manifest, markers = marker_manifest(tmp_path)

    proc = sil(heap(manifest, "--runner"), tools, tmp_path,
               {**heaptrack, "LD_PRELOAD": "libnothing.so"})

    assert proc.returncode == 1, proc.stderr
    assert "LD_PRELOAD is set" in proc.stderr
    assert not (tmp_path / "prof").exists()
    assert not list(markers.iterdir())


@needs_linux
def test_the_normalized_files_suffice_offline(tools, heaptrack, tmp_path):
    manifest, _ = marker_manifest(tmp_path)
    proc = sil(heap(manifest, "--runner"), tools, tmp_path, heaptrack)
    assert proc.returncode == 0, proc.stderr
    copy = tmp_path / "copy"
    (copy / "heap").mkdir(parents=True)
    for name in ("profile.json", "heap/allocations.json"):
        shutil.copy2(tmp_path / "prof" / name, copy / name)
    shutil.rmtree(tmp_path / "prof")
    shutil.rmtree(tools)

    report = profile(copy)
    document = json.loads((copy / "heap" / "allocations.json").read_text())
    modules = {m["id"] for m in report["capture"]["analysis"]["modules"]}
    assert all(f["module"] in modules or f["module"] is None
               for f in document["frames"])
    assert sum(row[2] for row in document["allocations"]) == \
        report["capture"]["analysis"]["totals"]["allocations"]
