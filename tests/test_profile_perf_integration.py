"""`sil profile` with the real Linux perf, on the qualified proof workload.

Opt-in: it runs only with SIL_PROFILE_PERF set, on the supported machine
class (Linux x86-64/glibc) with perf, cc and objcopy on PATH. The
profile-cpu-integration workflow runs it on ubuntu-24.04:

- SIL_PROFILE_PERF=denied, with the host default kernel.perf_event_paranoid
  (4 on Ubuntu): the command must stop in preflight with exit 1;
- SIL_PROFILE_PERF=available, after the disclosed change to 2: the full
  capture.

The workload is the one of the #314 proof (proofs/profiling/workload.py):
known hot functions in a stripped Native library with a separate debug file,
a shimmed Process participant that loads a library at run time and starts
short-lived children, and an FMU that the importer extracts into the Run
working directory. Presence checks need at least one sample; no check uses a
sample share or a time. SIL_PROFILE_EVIDENCE names a directory that keeps
the profile directories for upload.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT
from sil.profile_report import validate_profile_report

MODE = os.environ.get("SIL_PROFILE_PERF")
pytestmark = pytest.mark.skipif(
    MODE is None or platform.system() != "Linux"
    or platform.machine() != "x86_64",
    reason="opt-in real perf check: set SIL_PROFILE_PERF on Linux x86-64")

NATIVE = ("sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher")
HOT = {
    "native": ("silproof_native_spin_even", "silproof_native_spin_odd"),
    "process": ("silproof_process_spin",),
    "child": ("silproof_child_spin",),
    "fmu": ("silproof_fmu_spin",),
}


@pytest.fixture(scope="module")
def workload():
    # Dataclasses need the module in sys.modules, under its own name.
    name = "silproof_profile_workload"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "proofs" / "profiling" / "workload.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(workload, tmp_path_factory):
    return workload.build(tmp_path_factory.mktemp("workload"))


@pytest.fixture
def tools(build_dir, tmp_path) -> Path:
    directory = tmp_path / "tools"
    directory.mkdir()
    for name in NATIVE:
        (directory / name).symlink_to(build_dir / name)
    return directory


def profile_run(tools: Path, cwd: Path, manifest: Path, out: str,
                *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "PATH": os.pathsep.join([str(tools), "/usr/bin:/bin"])}
    return subprocess.run(
        [sys.executable, "-m", "sil.cli", "profile", str(manifest), "--out",
         out, "--collector", "cpu", "--participant-timeout-ms", "60000",
         *extra], cwd=cwd, env=env, capture_output=True, text=True,
        timeout=1800)


def keep(directory: Path, name: str) -> None:
    evidence = os.environ.get("SIL_PROFILE_EVIDENCE")
    if evidence:
        target = Path(evidence) / name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(directory, target, symlinks=True)


def write_manifest(workload, built, path: Path) -> Path:
    return workload.manifest(built, python=sys.executable).write(path).path


def with_debug_beside(built, debug: Path) -> None:
    shutil.copy2(debug, built.native.parent / built.native_debug.name)


def samples_document(out: Path) -> dict:
    return json.loads((out / "cpu" / "samples.json").read_text())


def leaf_symbols(out: Path) -> dict[str, int]:
    """Samples per leaf symbol, from the normalized samples."""
    document = samples_document(out)
    counts: dict[str, int] = {}
    for row in document["samples"]:
        frame = document["frames"][document["stacks"][row[4]][0]]
        counts[frame["symbol"]] = counts.get(frame["symbol"], 0) + 1
    return counts


@pytest.mark.skipif(MODE != "denied", reason="the permission-denied host")
def test_a_host_without_perf_permission_stops_in_preflight(
        workload, built, tools, tmp_path):
    manifest = write_manifest(workload, built, tmp_path / "manifest.json")

    proc = profile_run(tools, tmp_path, manifest, "prof")

    assert proc.returncode == 1, proc.stderr
    assert "perf cannot sample cpu-clock:u" in proc.stderr
    assert "perf_event_paranoid" in proc.stderr
    assert not (tmp_path / "prof").exists()
    assert not list(tmp_path.glob(".sil-run-*"))


@pytest.fixture(scope="module")
def matching(workload, built, build_dir, tmp_path_factory):
    """One capture with the matching debug file beside the library."""
    if MODE != "available":
        pytest.skip("the perf-capable host")
    cwd = tmp_path_factory.mktemp("matching")
    tools = cwd / "tools"
    tools.mkdir()
    for name in NATIVE:
        (tools / name).symlink_to(build_dir / name)
    with_debug_beside(built, built.native_debug)
    manifest = write_manifest(workload, built, cwd / "manifest.json")
    # The importer removes its extracted FMU at shutdown: the archive is the
    # copy of the FMU library.
    proc = profile_run(tools, cwd, manifest, "prof", "--modules-from",
                       str(built.fmu))
    keep(cwd / "prof", "matching")
    return cwd, proc


def test_the_capture_is_complete_and_valid(matching):
    cwd, proc = matching

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads((cwd / "prof" / "profile.json").read_text())
    assert validate_profile_report(report) == []
    assert report["run"]["status"] == "succeeded"
    assert report["capture"]["status"] == "complete"
    assert report["capture"]["totals"]["samples"] > 0
    assert report["capture"]["backend"]["version"].startswith("perf version")
    assert not list(cwd.glob(".sil-run-*"))


def test_every_known_hot_function_is_named(matching):
    cwd, _ = matching
    symbols = leaf_symbols(cwd / "prof")

    for functions in HOT.values():
        for function in functions:
            assert symbols.get(function, 0) > 0, (function, symbols)


def test_matching_debug_file_gives_source_lines_of_the_stripped_library(
        matching, built):
    cwd, _ = matching
    report = json.loads((cwd / "prof" / "profile.json").read_text())
    native, = [m for m in report["capture"]["modules"]
               if m["path"] == str(built.native)]

    assert native["debug"]["status"] == "separate_matching"
    assert native["build_id"]
    document = samples_document(cwd / "prof")
    lines = {frame["source"]["file"] for frame in document["frames"]
             if frame["module"] == native["id"] and frame["source"]}
    assert "silproof_native.c" in lines


def test_the_fmu_from_the_run_directory_is_captured_and_named(matching):
    cwd, _ = matching
    report = json.loads((cwd / "prof" / "profile.json").read_text())
    fmu = [m for m in report["capture"]["modules"]
           if m["path"].endswith("EgoMotion.so")]

    assert fmu and all(m["material"] == "captured_modules_from"
                       for m in fmu)
    assert all(m["build_id"] for m in fmu)
    assert any((cwd / "prof" / "cpu" / "buildid-cache").rglob("*"))


def test_short_lived_children_keep_their_process_identities(matching,
                                                            workload):
    cwd, _ = matching
    report = json.loads((cwd / "prof" / "profile.json").read_text())
    children = [p for p in report["capture"]["processes"]
                if "silproof_child" in p["commands"]]

    assert len(children) == workload.CHILDREN
    assert all(p["forked"] and p["exited"] and p["samples"] > 0
               for p in children)
    roles = [p["role"] for p in report["capture"]["processes"]]
    assert roles[:2] == ["launcher", "runner"]


def test_dwarf_unwinding_reaches_the_source_caller(matching):
    cwd, _ = matching
    document = samples_document(cwd / "prof")
    frames = document["frames"]
    callers = set()
    for row in document["samples"]:
        stack = document["stacks"][row[4]]
        if frames[stack[0]]["symbol"] == "silproof_native_spin_even" \
                and len(stack) > 1:
            callers.add(frames[stack[1]]["symbol"])

    assert "native_task" in callers


def test_inlined_frames_are_marked_and_belong_to_their_physical_frame(
        matching):
    cwd, _ = matching
    report = json.loads((cwd / "prof" / "profile.json").read_text())
    inlined = report["capture"]["symbolization"]["inlined_frames"]
    document = samples_document(cwd / "prof")
    frames = document["frames"]

    assert inlined["status"] == "available"
    with_inlined = 0
    for row in document["samples"]:
        stack = [frames[index] for index in document["stacks"][row[4]]]
        with_inlined += any(f.get("inlined") for f in stack)
        for position, frame in enumerate(stack):
            if frame.get("inlined"):
                # The next physical frame has the same address and module.
                physical = next(f for f in stack[position:]
                                if not f.get("inlined"))
                assert (frame["address"], frame["module"]) == (
                    physical["address"], physical["module"])
    assert with_inlined == inlined["samples_with_inlined_frames"]


def test_the_profiled_recording_equals_the_ordinary_one(matching, build_dir):
    cwd, _ = matching
    ordinary = subprocess.run(
        [str(build_dir / "sil-run"), str(cwd / "manifest.json"), "-o",
         str(cwd / "ordinary.mcap"), "--participant-timeout-ms", "60000"],
        cwd=cwd, capture_output=True, text=True)

    assert ordinary.returncode == 0, ordinary.stderr
    assert (cwd / "prof" / "run.mcap").read_bytes() == \
        (cwd / "ordinary.mcap").read_bytes()


def test_a_mismatched_debug_file_gives_no_name_and_no_source_line(
        workload, built, matching, tools, tmp_path):
    with_debug_beside(built, built.native_mismatched_debug)
    try:
        manifest = write_manifest(workload, built, tmp_path / "manifest.json")
        proc = profile_run(tools, tmp_path, manifest, "prof")
    finally:
        with_debug_beside(built, built.native_debug)
    keep(tmp_path / "prof", "mismatched")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads((tmp_path / "prof" / "profile.json").read_text())
    native, = [m for m in report["capture"]["modules"]
               if m["path"] == str(built.native)]
    assert native["debug"]["status"] == "separate_mismatched"
    assert native["samples_leaf"] > 0
    document = samples_document(tmp_path / "prof")
    native_frames = [f for f in document["frames"]
                     if f["module"] == native["id"]]
    assert all(f["source"] is None for f in native_frames)
    assert not {"silproof_native_spin_even", "silproof_native_spin_odd"} & {
        f["symbol"] for f in native_frames}
    # Without --modules-from, the removed FMU library keeps its samples,
    # build ID and offsets, and has no name.
    fmu = [m for m in report["capture"]["modules"]
           if m["path"].endswith("EgoMotion.so")]
    assert fmu and all(m["material"] == "unavailable" and m["build_id"]
                       for m in fmu)
    assert sum(m["samples_leaf"] for m in fmu) > 0
    # Nothing outside the profile directory names them (perf record -N).
    fmu_ids = {m["id"] for m in fmu}
    assert all(f["symbol"] is None for f in document["frames"]
               if f["module"] in fmu_ids)


# --- Activation correlation (#316) -------------------------------------------


@pytest.fixture(scope="module")
def correlated(workload, built, build_dir, tmp_path_factory):
    """One capture with --activations, on the same workload."""
    if MODE != "available":
        pytest.skip("the perf-capable host")
    cwd = tmp_path_factory.mktemp("correlated")
    tools = cwd / "tools"
    tools.mkdir()
    for name in NATIVE:
        (tools / name).symlink_to(build_dir / name)
    with_debug_beside(built, built.native_debug)
    manifest = write_manifest(workload, built, cwd / "manifest.json")
    proc = profile_run(tools, cwd, manifest, "prof", "--modules-from",
                       str(built.fmu), "--activations")
    keep(cwd / "prof", "correlated")
    return cwd, proc


def contexts_by_leaf(out: Path) -> dict[str, list]:
    """The correlation class of each sample, per leaf symbol."""
    document = samples_document(out)
    correlation = json.loads(
        (out / "activation" / "correlation.json").read_text())
    rows = correlation["activations"]
    found: dict[str, list] = {}
    for sample, (kind, ref) in zip(document["samples"],
                                   correlation["samples"]):
        frame = document["frames"][document["stacks"][sample[4]][0]]
        owner = rows[ref]["participant"] if kind in (
            "execution_context", "window_associated") else ref
        found.setdefault(frame["symbol"], []).append((kind, owner))
    return found


def test_the_join_is_proven_and_complete(correlated):
    cwd, proc = correlated

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads((cwd / "prof" / "profile.json").read_text())
    assert validate_profile_report(report) == []
    correlation = report["activation_correlation"]
    assert (correlation["status"], correlation["clock"]) == (
        "complete", "host_monotonic")
    mapping = {p["participant"]: p["mapping"]
               for p in correlation["summary"]["processes"]}
    assert mapping == {"fmu": "verified", "process": "verified"}


def test_native_hot_samples_carry_their_task_context(correlated):
    cwd, _ = correlated
    found = contexts_by_leaf(cwd / "prof")

    for function in HOT["native"]:
        assert found[function]
        assert set(found[function]) == {("execution_context", "native")}


def test_process_samples_are_window_associated_with_their_step(correlated):
    # The Clock shim makes the participant's own clock Virtual time; the
    # samples keep host time, so they fall inside the runner's host Step
    # intervals. The Recording check below shows that Participant time
    # stayed Virtual time in the same Run.
    cwd, _ = correlated
    found = contexts_by_leaf(cwd / "prof")

    for participant in ("process", "fmu"):
        for function in HOT[participant]:
            assert found[function]
            assert set(found[function]) == {
                ("window_associated", participant)}
    # A short-lived child belongs to the shimmed participant's tree; it is
    # never in the runner thread's execution context.
    assert found["silproof_child_spin"]
    assert {kind for kind, _ in found["silproof_child_spin"]} <= {
        "window_associated", "unattributed"}


def test_participant_time_stays_virtual_with_activations(correlated,
                                                         build_dir):
    # clock_now_ns in the Recording is the shimmed participant's own
    # CLOCK_MONOTONIC reading. Equal bytes to an ordinary Run show that it
    # is Virtual time: two host readings of two Runs cannot be equal.
    cwd, _ = correlated
    ordinary = subprocess.run(
        [str(build_dir / "sil-run"), str(cwd / "manifest.json"), "-o",
         str(cwd / "ordinary.mcap"), "--participant-timeout-ms", "60000"],
        cwd=cwd, capture_output=True, text=True)

    assert ordinary.returncode == 0, ordinary.stderr
    assert (cwd / "prof" / "run.mcap").read_bytes() == \
        (cwd / "ordinary.mcap").read_bytes()
