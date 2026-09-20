"""Run provenance side-cars at the runner boundary."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from conftest import BUILD_DIR, ROOT
from test_fmi import fmu_manifest
from toys import add_producer, add_thrower, toy_manifest


def sidecar(path: Path) -> dict:
    return json.loads(path.with_name(path.name + ".provenance.json").read_text())


def native_manifest(library: Path):
    manifest = toy_manifest(duration_ns=10_000_000)
    manifest.add_channel("ticks", schema="toy.Counter")
    manifest.add_native(
        "producer",
        library=str(library),
        config={"channel": "ticks", "period_ns": 10_000_000},
        publishes=["ticks"],
    )
    return manifest


def test_successful_record_binds_runner_process_and_machine(
    run_sil, sil_run, tmp_path
):
    script = tmp_path / "participant.sh"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"op\":\"ready\"}'\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"op\":\"step\"'* ) printf '%s\\n' '{\"op\":\"step_done\",\"out\":[]}' ;;\n"
        "    *'\"op\":\"shutdown\"'* ) exit 0 ;;\n"
        "  esac\n"
        "done\n"
    )
    script.chmod(0o755)

    manifest = toy_manifest(duration_ns=1)
    manifest.add_process(
        "process", command=[str(script)], step_period_ns=1, shim=True
    )
    ref = manifest.write(tmp_path / "manifest.json")
    out = tmp_path / "out.mcap"

    proc = run_sil(ref.path, out=out)
    assert proc.returncode == 0, proc.stderr

    document = sidecar(out)
    assert document["schema_version"] == 1
    assert document["manifest_hash"] == ref.hash
    assert document["run_exit_code"] == 0
    assert document["recording"]["sha256"] == hashlib.sha256(
        out.read_bytes()
    ).hexdigest()
    assert document["machine_class"]["id"] == "/".join(
        [
            document["machine_class"]["os"],
            document["machine_class"]["architecture"],
            document["machine_class"]["libc"],
        ]
    )

    runner = document["artifacts"]["runner"]
    assert runner["path"] == str(Path(sil_run).resolve())
    assert runner["sha256"] == hashlib.sha256(Path(runner["path"]).read_bytes()).hexdigest()

    [process] = document["artifacts"]["process_participants"]
    assert process["name"] == "process"
    assert process["command"][0] == str(script.resolve())
    assert process["executable"]["path"] == str(script.resolve())
    assert process["executable"]["sha256"] == hashlib.sha256(
        script.read_bytes()
    ).hexdigest()
    clock_shim = document["artifacts"]["clock_shim"]
    assert clock_shim["sha256"] == hashlib.sha256(
        Path(clock_shim["path"]).read_bytes()
    ).hexdigest()


def test_provenance_is_byte_deterministic_and_has_no_recording_path(
    run_sil, tmp_path
):
    script = tmp_path / "participant.sh"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"op\":\"ready\"}'\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"op\":\"step\"'* ) printf '%s\\n' '{\"op\":\"step_done\",\"out\":[]}' ;;\n"
        "    *'\"op\":\"shutdown\"'* ) exit 0 ;;\n"
        "  esac\n"
        "done\n"
    )
    script.chmod(0o755)
    manifest = toy_manifest(duration_ns=1)
    manifest.add_process("process", command=[str(script)], step_period_ns=1)
    ref = manifest.write(tmp_path / "manifest.json")

    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    assert run_sil(ref.path, out=first).returncode == 0
    assert run_sil(ref.path, out=second).returncode == 0
    assert first.with_name(first.name + ".provenance.json").read_bytes() == (
        second.with_name(second.name + ".provenance.json").read_bytes()
    )
    assert "first.mcap" not in first.with_name(
        first.name + ".provenance.json"
    ).read_text()


def test_participant_entries_are_sorted_by_name(run_sil, tmp_path):
    script = tmp_path / "participant.sh"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"op\":\"ready\"}'\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"op\":\"step\"'* ) printf '%s\\n' '{\"op\":\"step_done\",\"out\":[]}' ;;\n"
        "    *'\"op\":\"shutdown\"'* ) exit 0 ;;\n"
        "  esac\n"
        "done\n"
    )
    script.chmod(0o755)
    manifest = toy_manifest(duration_ns=1)
    manifest.add_process("zulu", command=[str(script)], step_period_ns=1)
    manifest.add_process("alpha", command=[str(script)], step_period_ns=1)
    ref = manifest.write(tmp_path / "manifest.json")
    out = tmp_path / "out.mcap"

    proc = run_sil(ref.path, out=out)
    assert proc.returncode == 0, proc.stderr
    document = sidecar(out)
    assert [participant["name"] for participant in document["artifacts"][
        "process_participants"
    ]] == ["alpha", "zulu"]


def test_replacing_native_bytes_changes_only_provenance_identity(
    run_sil, tmp_path
):
    library = tmp_path / "producer.silp"
    shutil.copy2(BUILD_DIR / "toy_producer.silp", library)
    manifest = native_manifest(library)
    ref = manifest.write(tmp_path / "manifest.json")

    first = tmp_path / "first.mcap"
    assert run_sil(ref.path, out=first).returncode == 0
    first_record = sidecar(first)

    library.write_bytes(library.read_bytes() + b"\nprovenance variant\n")
    second = tmp_path / "second.mcap"
    second_proc = run_sil(ref.path, out=second)
    assert second_proc.returncode == 0, second_proc.stderr
    second_record = sidecar(second)

    assert ref.hash == manifest.hash()
    first_library = first_record["artifacts"]["native_participants"][0]["library"]
    second_library = second_record["artifacts"]["native_participants"][0]["library"]
    assert first_library["path"] == second_library["path"] == str(library.resolve())
    assert first_library["sha256"] != second_library["sha256"]


def test_replacing_process_executable_bytes_changes_provenance(
    run_sil, tmp_path
):
    script = tmp_path / "participant.sh"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"op\":\"ready\"}'\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        "    *'\"op\":\"step\"'* ) printf '%s\\n' '{\"op\":\"step_done\",\"out\":[]}' ;;\n"
        "    *'\"op\":\"shutdown\"'* ) exit 0 ;;\n"
        "  esac\n"
        "done\n"
    )
    script.chmod(0o755)
    manifest = toy_manifest(duration_ns=1)
    manifest.add_process("process", command=[str(script)], step_period_ns=1)
    ref = manifest.write(tmp_path / "manifest.json")

    first = tmp_path / "first.mcap"
    assert run_sil(ref.path, out=first).returncode == 0
    first_digest = sidecar(first)["artifacts"]["process_participants"][0][
        "executable"
    ]["sha256"]

    script.write_bytes(script.read_bytes() + b"\n# changed bytes\n")
    second = tmp_path / "second.mcap"
    proc = run_sil(ref.path, out=second)
    assert proc.returncode == 0, proc.stderr
    second_digest = sidecar(second)["artifacts"]["process_participants"][0][
        "executable"
    ]["sha256"]
    assert ref.hash == manifest.hash()
    assert first_digest != second_digest


def test_replacing_fmu_bytes_changes_provenance(
    run_sil, tmp_path
):
    fmu = tmp_path / "model.fmu"
    shutil.copy2(ROOT / "tests/fixtures/reference-fmus/3.0/Feedthrough.fmu", fmu)
    manifest = fmu_manifest(fmu=fmu)
    ref = manifest.write(tmp_path / "manifest.json")

    first = tmp_path / "first.mcap"
    assert run_sil(ref.path, out=first).returncode == 0
    first_digest = sidecar(first)["artifacts"]["fmus"][0]["archive"]["sha256"]

    fmu.write_bytes(fmu.read_bytes() + b"provenance variant")
    second = tmp_path / "second.mcap"
    proc = run_sil(ref.path, out=second)
    assert proc.returncode == 0, proc.stderr
    second_digest = sidecar(second)["artifacts"]["fmus"][0]["archive"]["sha256"]
    assert ref.hash == manifest.hash()
    assert first_digest != second_digest


def test_failed_run_keeps_resolved_artifacts_in_sidecar(run_sil, tmp_path):
    manifest = toy_manifest(duration_ns=10_000_000)
    add_thrower(manifest, throw_in="task", kind="std", period_ns=10_000_000)
    ref = manifest.write(tmp_path / "manifest.json")
    out = tmp_path / "failed.mcap"

    proc = run_sil(ref.path, out=out)
    assert proc.returncode == 1
    document = sidecar(out)
    assert document["run_exit_code"] == 1
    assert document["manifest_hash"] == ref.hash
    assert document["artifacts"]["native_participants"][0]["library"][
        "sha256"
    ]


def test_no_recording_still_emits_a_sidecar(run_sil, sil_run, tmp_path):
    manifest = toy_manifest(duration_ns=1)
    path = manifest.write(tmp_path / "manifest.json").path
    proc = subprocess.run(
        [str(sil_run), str(path), "--no-recording"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "out.mcap").exists()
    document = json.loads(
        (tmp_path / "out.mcap.provenance.json").read_text()
    )
    assert document["recording"] is None
    assert document["run_exit_code"] == 0


def test_unreadable_artifact_is_exit_two_and_explained_in_sidecar(
    run_sil, tmp_path
):
    manifest = toy_manifest(duration_ns=1)
    manifest.add_process(
        "missing",
        command=[str(tmp_path / "does-not-exist")],
        step_period_ns=1,
    )
    ref = manifest.write(tmp_path / "manifest.json")
    out = tmp_path / "missing.mcap"

    proc = run_sil(ref.path, out=out)
    assert proc.returncode == 2
    assert "does-not-exist" in proc.stderr
    document = sidecar(out)
    assert document["run_exit_code"] == 2
    assert any("Process participant 'missing'" in error["artifact"]
               for error in document["errors"])
