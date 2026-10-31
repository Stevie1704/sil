"""Cross-feature qualification of the provenance policy (#329).

Each focused ticket tests one command. These tests hold one fixture steady and
change the command and the policy. The expected Recording bytes come from the
ordinary `sil-run` in `full` mode, and digests come from `hashlib`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from test_profile_heap_run import heap, heaptrack, needs_linux  # noqa: F401
from test_profile_heap_run import sil as heap_sil
from test_profile_run import exports, sil as profile_sil, tools  # noqa: F401
from test_provenance_mode import (
    mixed_manifest, native_only_manifest, process_manifest, replay_run, run,
)
from test_run_diagnostics_cli import native_path, sil  # noqa: F401

MODES = ["full", "external"]
FIXTURES = {"native": native_only_manifest, "process": process_manifest,
            "mixed": mixed_manifest}


def policy(mode: str) -> list[str]:
    return ["--provenance-mode", mode]


def sil_run_outcome(sil_run, manifest: Path, out: Path, mode: str) -> int:
    return run(sil_run, manifest, out, *policy(mode)).returncode


def assert_equal_recording(reference: Path, recording: Path) -> None:
    assert recording.read_bytes() == reference.read_bytes()


def assert_policy_of_side_car(side_car: Path, recording: Path, mode: str):
    document = json.loads(side_car.read_text())
    assert document["provenance_mode"] == mode
    digest = document["recording"]
    if mode == "external":
        assert digest == {"digest_state": "omitted", "sha256": None}
    else:
        assert digest["digest_state"] == "calculated"
        assert digest["sha256"] == hashlib.sha256(
            recording.read_bytes()).hexdigest()


@pytest.fixture(params=FIXTURES)
def stable_input(request, sil_run, tmp_path):
    """A Manifest with its reference Recording."""
    manifest = FIXTURES[request.param](tmp_path).path
    reference = tmp_path / "reference.mcap"
    assert sil_run_outcome(sil_run, manifest, reference, "full") == 0
    return manifest, reference


@pytest.fixture
def replay_input(sil_run, run_sil, tmp_path):
    ref, _source = replay_run(tmp_path, run_sil)
    reference = tmp_path / "reference.mcap"
    assert sil_run_outcome(sil_run, ref.path, reference, "full") == 0
    return ref.path, reference


@pytest.mark.parametrize("mode", MODES)
def test_sil_run_writes_the_reference_bytes(
        stable_input, sil_run, tmp_path, mode):
    manifest, reference = stable_input
    out = tmp_path / "out.mcap"

    assert sil_run_outcome(sil_run, manifest, out, mode) == 0

    assert_equal_recording(reference, out)
    assert_policy_of_side_car(Path(f"{out}.provenance.json"), out, mode)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("diagnostics", [[], ["--diagnostics"]],
                         ids=["ordinary", "diagnostics"])
def test_the_run_command_writes_the_reference_bytes(
        stable_input, native_path, tmp_path, mode, diagnostics):  # noqa: F811
    manifest, reference = stable_input

    proc = sil(["run", str(manifest), "-o", "out.mcap", *diagnostics,
                *policy(mode)], native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "out.mcap"
    assert_equal_recording(reference, out)
    assert_policy_of_side_car(tmp_path / "out.mcap.provenance.json", out, mode)


@pytest.mark.parametrize("mode", MODES)
def test_the_benchmark_writes_the_reference_bytes_in_each_run(
        stable_input, native_path, tmp_path, mode):  # noqa: F811
    manifest, reference = stable_input

    proc = sil(["benchmark", str(manifest), "--repeats", "2", "--warmup", "0",
                "--out", "bench", *policy(mode)], native_path, tmp_path)

    assert proc.returncode == 0, proc.stderr
    summary = json.loads((tmp_path / "bench" / "summary.json").read_text())
    assert len(summary["runs"]) == 2
    for entry in summary["runs"]:
        directory = tmp_path / "bench" / entry["directory"]
        out = directory / "run.mcap"
        assert_equal_recording(reference, out)
        assert_policy_of_side_car(directory / "run.mcap.provenance.json", out,
                                  mode)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("activations", [[], ["--activations"]],
                         ids=["plain", "activations"])
def test_a_cpu_capture_writes_the_reference_bytes(
        stable_input, tools, exports, tmp_path, mode,  # noqa: F811
        activations):
    manifest, reference = stable_input

    proc = profile_sil([str(manifest), "--out", "prof", "--collector", "cpu",
                        *activations, *policy(mode)], tools, tmp_path, exports)

    # The fake perf cannot prove an Activation join, so only the Run must pass.
    report = json.loads((tmp_path / "prof" / "profile.json").read_text())
    assert report["run"]["status"] == "succeeded", proc.stderr
    out = tmp_path / "prof" / "run.mcap"
    assert_equal_recording(reference, out)
    assert_policy_of_side_car(
        tmp_path / "prof" / "run.mcap.provenance.json", out, mode)


@needs_linux
@pytest.mark.parametrize("mode", MODES)
def test_a_runner_heap_capture_writes_the_reference_bytes(
        stable_input, tools, heaptrack, tmp_path, mode):  # noqa: F811
    manifest, reference = stable_input

    proc = heap_sil([*heap(manifest, "--runner"), *policy(mode)], tools,
                    tmp_path, heaptrack)

    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "prof" / "run.mcap"
    assert_equal_recording(reference, out)
    assert_policy_of_side_car(
        tmp_path / "prof" / "run.mcap.provenance.json", out, mode)


@needs_linux
@pytest.mark.parametrize("mode", MODES)
def test_a_process_heap_capture_writes_the_reference_bytes(
        sil_run, tools, heaptrack, tmp_path, mode):  # noqa: F811
    manifest = process_manifest(tmp_path).path
    reference = tmp_path / "reference.mcap"
    assert sil_run_outcome(sil_run, manifest, reference, "full") == 0

    proc = heap_sil([*heap(manifest, "--participant", "process"),
                     *policy(mode)], tools, tmp_path, heaptrack)

    assert proc.returncode == 0, proc.stderr
    assert_equal_recording(reference, tmp_path / "prof" / "run.mcap")


# --- Replay --------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_a_replay_writes_the_reference_bytes_in_each_command(
        replay_input, native_path, tools, exports, tmp_path,  # noqa: F811
        mode):
    manifest, reference = replay_input

    ran = sil(["run", str(manifest), "-o", "run.mcap", "--diagnostics",
               *policy(mode)], native_path, tmp_path)
    captured = profile_sil([str(manifest), "--out", "prof", "--collector",
                            "cpu", *policy(mode)], tools, tmp_path, exports)

    assert ran.returncode == 0, ran.stderr
    assert captured.returncode == 0, captured.stderr
    assert_equal_recording(reference, tmp_path / "run.mcap")
    assert_equal_recording(reference, tmp_path / "prof" / "run.mcap")


def append_byte(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"\0")


def truncate(path: Path) -> None:
    path.write_bytes(path.read_bytes()[:-1])


def flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))


def remove(path: Path) -> None:
    path.unlink()


MALFORMED = {"appended": append_byte, "truncated": truncate,
             "flipped": flip_byte, "removed": remove}


@pytest.fixture(params=MALFORMED)
def malformed_replay(request, run_sil, tmp_path):
    ref, source = replay_run(tmp_path, run_sil)
    MALFORMED[request.param](source)
    return ref.path


@pytest.mark.parametrize("mode", MODES)
def test_sil_run_rejects_a_malformed_replay(
        malformed_replay, sil_run, tmp_path, mode):
    out = tmp_path / "out.mcap"

    assert sil_run_outcome(sil_run, malformed_replay, out, mode) == 2

    assert json.loads(Path(f"{out}.provenance.json").read_text())[
        "provenance_mode"] == mode


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("diagnostics", [[], ["--diagnostics"]],
                         ids=["ordinary", "diagnostics"])
def test_the_run_command_rejects_a_malformed_replay(
        malformed_replay, native_path, tmp_path, mode,  # noqa: F811
        diagnostics):
    proc = sil(["run", str(malformed_replay), "-o", "out.mcap", *diagnostics,
                *policy(mode)], native_path, tmp_path)

    assert proc.returncode == 2


@pytest.mark.parametrize("mode", MODES)
def test_a_cpu_capture_rejects_a_malformed_replay(
        malformed_replay, tools, exports, tmp_path, mode):  # noqa: F811
    proc = profile_sil([str(malformed_replay), "--out", "prof", "--collector",
                        "cpu", *policy(mode)], tools, tmp_path, exports)

    assert proc.returncode == 2
    assert json.loads((tmp_path / "prof" / "profile.json").read_text())[
        "run"]["status"] == "configuration_error"


@needs_linux
@pytest.mark.parametrize("mode", MODES)
def test_a_heap_capture_rejects_a_malformed_replay(
        malformed_replay, tools, heaptrack, tmp_path, mode):  # noqa: F811
    proc = heap_sil([*heap(malformed_replay, "--runner"), *policy(mode)],
                    tools, tmp_path, heaptrack)

    assert proc.returncode == 2
    assert json.loads((tmp_path / "prof" / "profile.json").read_text())[
        "run"]["status"] == "configuration_error"


# --- A policy does not reach other correctness hashes ---------------------


def test_replacing_a_library_changes_only_the_full_mode_digest(
        sil_run, tmp_path):
    """The Manifest hash is configuration identity: a platform must bind the
    artifacts itself. A subsequent Run observes the replaced bytes."""
    ref = native_only_manifest(tmp_path)
    library = tmp_path / "producer.silp"

    def side_car(name: str, mode: str) -> dict:
        out = tmp_path / name
        assert sil_run_outcome(sil_run, ref.path, out, mode) == 0
        return json.loads(Path(f"{out}.provenance.json").read_text())

    before = side_car("before.mcap", "full")
    with library.open("ab") as file:
        file.write(b"appended bytes that the loader ignores")
    after = side_car("after.mcap", "full")
    external = side_car("external.mcap", "external")

    assert before["manifest_hash"] == after["manifest_hash"] \
        == external["manifest_hash"]
    expected = hashlib.sha256(library.read_bytes()).hexdigest()
    native = [entry["library"]["sha256"] for entry
              in after["artifacts"]["native_participants"]]
    assert native == [expected]
    assert [entry["library"]["sha256"] for entry
            in before["artifacts"]["native_participants"]] != [expected]
    assert [entry["library"]["digest_state"] for entry
            in external["artifacts"]["native_participants"]] == ["omitted"]


def test_the_manifest_hash_is_of_the_exact_bytes(sil_run, tmp_path):
    ref = native_only_manifest(tmp_path)
    reformatted = tmp_path / "reformatted.json"
    shutil.copy2(ref.path, reformatted)
    reformatted.write_bytes(reformatted.read_bytes() + b"\n")

    for manifest, name in ((ref.path, "a.mcap"), (reformatted, "b.mcap")):
        assert sil_run_outcome(sil_run, manifest, tmp_path / name,
                               "external") == 0
        document = json.loads(
            (tmp_path / f"{name}.provenance.json").read_text())
        assert document["manifest_hash"] == hashlib.sha256(
            manifest.read_bytes()).hexdigest()
