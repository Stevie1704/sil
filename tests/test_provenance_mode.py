"""`--provenance-mode` at the runner boundary (#327).

Who owns artifact identity is an invocation policy. It changes which digests
the side-car holds, and no other result of the Run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import BUILD_DIR
from test_provenance import STEP_LOOP, native_manifest, sidecar
from test_replayer import record_producer_run, replay_manifest
from toys import toy_manifest


def run(runner: Path, manifest: Path, out: Path | None, *options: str,
        env: dict | None = None) -> subprocess.CompletedProcess:
    destination = ["--no-recording"] if out is None else ["-o", str(out)]
    return subprocess.run(
        [str(runner), str(manifest), *destination, *options],
        capture_output=True, text=True, cwd=manifest.parent,
        env={**os.environ, **(env or {})})


def executable_script(directory: Path) -> Path:
    script = directory / "participant.sh"
    script.write_text(STEP_LOOP)
    script.chmod(0o755)
    return script


def mixed_manifest(directory: Path):
    """A Native participant, and a Process participant whose command names a
    file, with the Clock shim: every artifact kind of a side-car."""
    library = directory / "producer.silp"
    shutil.copy2(BUILD_DIR / "toy_producer.silp", library)
    payload = directory / "scenario.xosc"
    payload.write_text("<OpenSCENARIO/>\n")
    manifest = native_manifest(library)
    manifest.add_process("process", command=[
        str(executable_script(directory)), str(payload)],
        step_period_ns=10_000_000, shim=True)
    return manifest.write(directory / "manifest.json")


def artifacts_of(document: dict) -> list[dict]:
    artifacts = document["artifacts"]
    return [artifacts["runner"], artifacts["clock_shim"],
            *(p["library"] for p in artifacts["native_participants"]),
            *(p["executable"] for p in artifacts["process_participants"]),
            *(f["file"] for f in artifacts["command_files"])]


@pytest.mark.parametrize("options", [(), ("--provenance-mode", "full")],
                         ids=["default", "explicit"])
def test_full_mode_digests_every_artifact_and_the_recording(
        sil_run, tmp_path, options):
    ref = mixed_manifest(tmp_path)
    out = tmp_path / "out.mcap"

    proc = run(sil_run, ref.path, out, *options)

    assert proc.returncode == 0, proc.stderr
    document = sidecar(out)
    assert document["schema_version"] == 2
    assert document["provenance_mode"] == "full"
    artifacts = artifacts_of(document)
    assert len(artifacts) == 5
    for artifact in artifacts:
        assert artifact["digest_state"] == "calculated"
        assert artifact["sha256"] == hashlib.sha256(
            Path(artifact["path"]).read_bytes()).hexdigest()
    assert document["recording"] == {
        "digest_state": "calculated",
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}


def test_external_mode_keeps_paths_and_marks_every_digest_omitted(
        sil_run, tmp_path):
    ref = mixed_manifest(tmp_path)
    full, external = tmp_path / "full.mcap", tmp_path / "external.mcap"
    assert run(sil_run, ref.path, full).returncode == 0

    proc = run(sil_run, ref.path, external, "--provenance-mode", "external")

    assert proc.returncode == 0, proc.stderr
    document = sidecar(external)
    assert document["schema_version"] == 2
    assert document["provenance_mode"] == "external"
    assert document["manifest_hash"] == ref.hash
    for artifact, reference in zip(artifacts_of(document),
                                   artifacts_of(sidecar(full)), strict=True):
        assert artifact == {"path": reference["path"],
                            "digest_state": "omitted", "sha256": None}
    assert document["recording"] == {"digest_state": "omitted", "sha256": None}


def hash_work(instrumented: Path, ref, out: Path, *options: str) -> dict:
    report = out.with_name(out.name + ".counters.json")
    proc = run(instrumented, ref.path, out, *options,
               env={"SIL_COPY_COUNTERS_OUT": str(report)})
    assert proc.returncode == 0, proc.stderr
    return json.loads(report.read_text())["deterministic"]["provenance_hash"]


def test_external_mode_performs_no_provenance_hashing(
        sil_run_instrumented, tmp_path):
    ref = mixed_manifest(tmp_path)

    full = hash_work(sil_run_instrumented, ref, tmp_path / "full.mcap")
    external = hash_work(sil_run_instrumented, ref, tmp_path / "ext.mcap",
                         "--provenance-mode", "external")

    # Runner, shim, library, executable and command file. The Recording digest
    # comes from the write path, so it is not a pass over a file.
    assert full["count"] == 5
    assert external == {"count": 0, "bytes": 0}


def shared_inputs_manifest(directory: Path):
    """Two Process participants that share one executable and one command file,
    the second through a symbolic link."""
    script = executable_script(directory)
    payload = directory / "scenario.xosc"
    payload.write_text("<OpenSCENARIO/>\n")
    alias = directory / "alias.xosc"
    alias.symlink_to(payload.name)
    manifest = toy_manifest(duration_ns=10_000_000)
    for name, argument in (("first", payload), ("second", alias)):
        manifest.add_process(name, command=[str(script), str(argument)],
                             step_period_ns=10_000_000)
    return manifest.write(directory / "shared.json")


def test_shared_inputs_count_real_hashes_only_and_none_when_external(
        sil_run_instrumented, tmp_path):
    ref = shared_inputs_manifest(tmp_path)
    full_out, external_out = tmp_path / "full.mcap", tmp_path / "ext.mcap"

    full = hash_work(sil_run_instrumented, ref, full_out)
    external = hash_work(sil_run_instrumented, ref, external_out,
                         "--provenance-mode", "external")

    # Runner, executable and command file: a reused digest does not count, and
    # the Recording digest is not a pass over a file.
    assert full["count"] == 3
    assert external == {"count": 0, "bytes": 0}

    # Every Participant keeps its own entries in both modes.
    payload = str((tmp_path / "scenario.xosc").resolve())
    reference = hashlib.sha256(Path(payload).read_bytes()).hexdigest()
    for out, state, sha256 in ((full_out, "calculated", reference),
                               (external_out, "omitted", None)):
        artifacts = sidecar(out)["artifacts"]
        assert len(artifacts["process_participants"]) == 2
        assert [(entry["participant"], entry["file"])
                for entry in artifacts["command_files"]] == [
            (name, {"path": payload, "digest_state": state, "sha256": sha256})
            for name in ("first", "second")]


def replay_run(directory: Path, run_sil):
    source, _ = record_producer_run(run_sil, directory)
    return replay_manifest(source).write(directory / "replay.json"), source


def process_manifest(directory: Path):
    manifest = toy_manifest(duration_ns=10_000_000)
    manifest.add_process("process", command=[str(executable_script(directory))],
                         step_period_ns=10_000_000)
    return manifest.write(directory / "process.json")


def native_only_manifest(directory: Path):
    library = directory / "producer.silp"
    shutil.copy2(BUILD_DIR / "toy_producer.silp", library)
    return native_manifest(library).write(directory / "native.json")


@pytest.mark.parametrize("build", [native_only_manifest, process_manifest,
                                   mixed_manifest],
                         ids=["native", "process", "mixed"])
def test_the_policy_does_not_change_recording_bytes(sil_run, tmp_path, build):
    ref = build(tmp_path)
    full, external = tmp_path / "full.mcap", tmp_path / "external.mcap"

    assert run(sil_run, ref.path, full).returncode == 0
    proc = run(sil_run, ref.path, external, "--provenance-mode", "external")

    assert proc.returncode == 0, proc.stderr
    assert full.read_bytes() == external.read_bytes()


def test_the_policy_does_not_change_a_replay_recording(sil_run, run_sil,
                                                       tmp_path):
    ref, _ = replay_run(tmp_path, run_sil)
    full, external = tmp_path / "full.mcap", tmp_path / "external.mcap"

    assert run(sil_run, ref.path, full).returncode == 0
    proc = run(sil_run, ref.path, external, "--provenance-mode", "external")

    assert proc.returncode == 0, proc.stderr
    assert full.read_bytes() == external.read_bytes()


@pytest.mark.parametrize("mode", ["full", "external"])
def test_replay_hash_mismatch_is_rejected_in_both_modes(sil_run, run_sil,
                                                        tmp_path, mode):
    ref, source = replay_run(tmp_path, run_sil)
    source.write_bytes(source.read_bytes() + b"\0")
    out = tmp_path / "out.mcap"

    proc = run(sil_run, ref.path, out, "--provenance-mode", mode)

    assert proc.returncode == 2, proc.stderr
    assert "hash" in proc.stderr.lower()
    assert sidecar(out)["provenance_mode"] == mode


@pytest.mark.parametrize("mode", ["full", "external"])
def test_no_recording_is_not_an_omitted_recording(sil_run, tmp_path, mode):
    ref = mixed_manifest(tmp_path)

    proc = run(sil_run, ref.path, None, "--provenance-mode", mode)

    assert proc.returncode == 0, proc.stderr
    document = json.loads((tmp_path / "out.mcap.provenance.json").read_text())
    assert document["recording"] == {"digest_state": "not_requested",
                                     "sha256": None}


def test_a_custom_side_car_path_is_kept_in_external_mode(sil_run, tmp_path):
    ref = mixed_manifest(tmp_path)
    custom = tmp_path / "custom.json"

    proc = run(sil_run, ref.path, None, "--provenance-mode", "external",
               "--provenance", str(custom))

    assert proc.returncode == 0, proc.stderr
    assert json.loads(custom.read_text())["provenance_mode"] == "external"
    assert not (tmp_path / "out.mcap.provenance.json").exists()


def missing_executable_manifest(directory: Path):
    manifest = toy_manifest(duration_ns=1)
    manifest.add_process("missing", command=[str(directory / "does-not-exist")],
                         step_period_ns=1)
    return manifest.write(directory / "manifest.json")


@pytest.mark.parametrize("mode", ["full", "external"])
def test_a_missing_artifact_is_unresolved_in_both_modes(sil_run, tmp_path,
                                                        mode):
    ref = missing_executable_manifest(tmp_path)
    out = tmp_path / "out.mcap"

    proc = run(sil_run, ref.path, out, "--provenance-mode", mode)

    assert proc.returncode == 2
    assert "does-not-exist" in proc.stderr
    document = sidecar(out)
    assert document["run_exit_code"] == 2
    [process] = document["artifacts"]["process_participants"]
    executable = process["executable"]
    assert executable["path"] == str(tmp_path / "does-not-exist")
    assert executable["digest_state"] == "unresolved"
    assert executable["sha256"] is None
    assert "does-not-exist" in executable["reason"]
    assert any("Process participant 'missing'" in error["artifact"]
               for error in document["errors"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("mode", ["full", "external"])
def test_an_unreadable_artifact_fails_preparation_in_both_modes(
        sil_run, tmp_path, mode):
    library = tmp_path / "producer.silp"
    shutil.copy2(BUILD_DIR / "toy_producer.silp", library)
    ref = native_manifest(library).write(tmp_path / "manifest.json")
    library.chmod(0o000)
    out = tmp_path / "out.mcap"

    try:
        proc = run(sil_run, ref.path, out, "--provenance-mode", mode)
    finally:
        library.chmod(0o644)

    assert proc.returncode == 2, proc.stderr
    record = sidecar(out)["artifacts"]["native_participants"][0]["library"]
    assert record["path"] == str(library.resolve())
    assert record["digest_state"] == "failed"
    assert record["sha256"] is None
    assert record["reason"]


def test_a_failed_recording_digest_is_not_an_omitted_one(sil_run, tmp_path):
    """A Manifest error leaves no Recording file to digest in full mode."""
    ref = missing_executable_manifest(tmp_path)
    full, external = tmp_path / "full.mcap", tmp_path / "external.mcap"

    run(sil_run, ref.path, full)
    run(sil_run, ref.path, external, "--provenance-mode", "external")

    recording = sidecar(full)["recording"]
    assert recording["digest_state"] == "failed"
    assert recording["sha256"] is None
    assert recording["reason"]
    assert sidecar(external)["recording"] == {"digest_state": "omitted",
                                              "sha256": None}


@pytest.mark.parametrize("options", [
    ("--provenance-mode", "none"),
    ("--provenance-mode", ""),
    ("--provenance-mode",),
    ("--provenance-mode", "full", "--provenance-mode", "full"),
    ("--provenance-mode", "full", "--provenance-mode", "external"),
], ids=["unknown", "empty", "no-value", "repeated", "conflicting"])
@pytest.mark.parametrize("runner", ["sil_run", "sil_run_diagnostics"])
def test_invalid_mode_is_a_usage_error_before_any_run(
        request, tmp_path, runner, options):
    ref = mixed_manifest(tmp_path)
    out = tmp_path / "out.mcap"

    proc = run(request.getfixturevalue(runner), ref.path, out, *options)

    assert proc.returncode == 2
    assert not out.exists()
    assert not (tmp_path / "out.mcap.provenance.json").exists()


def test_each_policy_is_deterministic_and_the_policies_differ(sil_run,
                                                              tmp_path):
    ref = mixed_manifest(tmp_path)
    bytes_of = {}
    for mode in ("full", "external"):
        records = []
        for index in (1, 2):
            out = tmp_path / f"{mode}-{index}.mcap"
            assert run(sil_run, ref.path, out,
                       "--provenance-mode", mode).returncode == 0
            records.append(out.with_name(out.name + ".provenance.json")
                           .read_bytes())
        assert records[0] == records[1]
        bytes_of[mode] = records[0]
    assert bytes_of["full"] != bytes_of["external"]


def test_the_policy_is_no_manifest_field(sil_run, tmp_path):
    ref = mixed_manifest(tmp_path)
    before = ref.path.read_bytes()

    run(sil_run, ref.path, tmp_path / "out.mcap",
        "--provenance-mode", "external")

    assert ref.path.read_bytes() == before
    assert b"provenance" not in before
