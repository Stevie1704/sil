"""Offline regression bundles at the `sil-bundle` boundary (issue #200).

Every bundle here is prepared from the source tree and then sealed, verified
and run by the staged installation: the wheel in its own venv and the CMake
install prefix. The declared environment names only those two directories,
so no source checkout, compiler, exporter or independent importer is
reachable from a Run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT, load_module

prepare = load_module("bundle_prepare", ROOT / "examples" / "bundle" / "prepare.py")


@pytest.fixture(scope="module")
def runtime(installed_python: Path, staged_prefix: Path):
    return prepare.Runtime(installed_python / "bin", staged_prefix / "bin")


@pytest.fixture(scope="module")
def library_binary(build_dir: Path) -> Path:
    path = build_dir / "speed_filter.so"
    assert path.is_file(), f"library build missing at {path}"
    return path


def sil_bundle(runtime, *args) -> subprocess.CompletedProcess:
    """The installed command, with no source tree on any path."""
    env = {"PATH": f"{runtime.python_bin}:{runtime.sil_bin}", "PYTHONNOUSERSITE": "1"}
    return subprocess.run([str(runtime.python_bin / "sil-bundle"), *map(str, args)],
                          env=env, capture_output=True, text=True)


def sealed(runtime, root: Path) -> Path:
    proc = sil_bundle(runtime, "seal", root)
    assert proc.returncode == 0, proc.stderr
    return root


def summary(evidence: Path) -> dict:
    return json.loads((evidence / "summary.json").read_text())


def digests(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def library_bundle(tmp_path, runtime, library_binary) -> Path:
    prepare.library(tmp_path / "library", runtime, library_binary)
    return sealed(runtime, tmp_path / "library")


def test_library_bundle_runs_offline_and_keeps_its_inputs(
        library_bundle, runtime, tmp_path):
    before = digests(library_bundle)
    evidence = tmp_path / "evidence"

    proc = sil_bundle(runtime, "run", library_bundle, "-o", evidence)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = summary(evidence)
    assert result["verdict"] == "pass"
    assert result["bundle_unchanged"] is True
    assert result["runtime"]["version"] == "0.1.0"
    assert "does not close" in result["dependency_closure"]
    (run,) = result["runs"]
    assert run["exit_code"] == 0
    assert run["determinism"]["verdict"] == "pass"
    runs = evidence / "runs" / "library"
    for name in ("run-1.mcap", "run-2.mcap", "run-1.provenance.json", "run-1.log"):
        assert (runs / name).is_file()
    provenance = json.loads((runs / "run-1.provenance.json").read_text())
    assert provenance["manifest_hash"] == run["manifest_sha256"]
    assert digests(library_bundle) == before
    assert str(ROOT) not in json.dumps(result)


def test_seal_records_the_dependencies_a_manifest_hash_does_not_cover(
        library_bundle, runtime):
    lock = json.loads((library_bundle / "bundle.lock.json").read_text())

    python = lock["dependencies"]["python"]
    assert set(python["modules"]) == set(prepare.MODULES)
    assert python["modules"]["sil"]["sha256"]
    assert str(ROOT) not in json.dumps(python["modules"]["sil"]["origin"])
    assert lock["dependencies"]["runner"]["build_info"]["version"] == "0.1.0"
    assert set(lock["contents_sha256"]) >= {
        "bundle.json", "library.json", "speed_filter.so", "adapter.py",
        "binding.py", "signals.csv", "signals.mcap"}


def test_fmu_replay_bundle_passes_its_prepared_reference(
        tmp_path, runtime, build_dir):
    root = prepare.fmu_replay(tmp_path / "fmu", runtime,
                              build_dir / "EgoMotion.so").parent
    sealed(runtime, root)

    proc = sil_bundle(runtime, "run", root, "-o", tmp_path / "evidence")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    (run,) = summary(tmp_path / "evidence")["runs"]
    assert run["comparisons"]["reference"] == {
        "verdict": "pass", "divergences": 0, "coverage_problems": 0}
    report = tmp_path / "evidence" / "runs" / "fmu-replay" / "reference.comparison.json"
    assert json.loads(report.read_text())["verdict"] == "pass"


def test_coupled_bundle_runs_the_loop_and_its_replacement(
        tmp_path, runtime, build_dir, sil_run):
    fmu = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
    root = prepare.coupling(tmp_path / "coupling", runtime, fmu, sil_run).parent
    sealed(runtime, root)

    proc = sil_bundle(runtime, "run", root, "-o", tmp_path / "evidence")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    runs = {r["name"]: r for r in summary(tmp_path / "evidence")["runs"]}
    assert set(runs) == {"coupled", "substituted"}
    for run in runs.values():
        assert run["determinism"]["verdict"] == "pass"
        assert run["comparisons"]["retained"]["verdict"] == "pass"


def test_a_tampered_artifact_refuses_the_bundle_before_any_run(
        library_bundle, runtime, tmp_path):
    csv = library_bundle / "signals.csv"
    csv.write_text(csv.read_text().replace("8", "9", 1))

    proc = sil_bundle(runtime, "run", library_bundle, "-o", tmp_path / "evidence")

    assert proc.returncode == 2
    assert "altered artifact signals.csv" in proc.stderr
    result = summary(tmp_path / "evidence")
    assert result["verdict"] == "refused"
    assert result["runs"] == []
    assert not (tmp_path / "evidence" / "runs").exists()


def test_an_unsealed_file_in_the_bundle_refuses_it(library_bundle, runtime):
    (library_bundle / "extra.py").write_text("")

    proc = sil_bundle(runtime, "verify", library_bundle)

    assert proc.returncode == 2
    assert "unsealed file extra.py" in proc.stderr


def test_a_missing_dependency_refuses_the_bundle(
        tmp_path, runtime, library_binary):
    vendor = tmp_path / "vendor" / "speed_filter.so"
    vendor.parent.mkdir()
    shutil.copyfile(library_binary, vendor)
    root = prepare.library(tmp_path / "library", runtime, vendor, bundled=False).parent
    sealed(runtime, root)
    vendor.unlink()

    proc = sil_bundle(runtime, "run", root, "-o", tmp_path / "evidence")

    assert proc.returncode == 2
    assert f"missing dependency: file {vendor.resolve()}" in proc.stderr


def test_a_changed_dependency_refuses_the_bundle(tmp_path, runtime, library_binary):
    vendor = tmp_path / "vendor" / "speed_filter.so"
    vendor.parent.mkdir()
    shutil.copyfile(library_binary, vendor)
    root = prepare.library(tmp_path / "library", runtime, vendor, bundled=False).parent
    sealed(runtime, root)
    with vendor.open("ab") as f:
        f.write(b"\0")

    proc = sil_bundle(runtime, "verify", root)

    assert proc.returncode == 2
    assert f"file {vendor.resolve()} differs from its seal" in proc.stderr


def test_a_reference_mismatch_fails_the_comparison(tmp_path, runtime, build_dir):
    # A reference prepared wrongly: 0.1 m/s too fast at 0.5 s.
    source = (ROOT / "examples" / "fmu-replay" / "reference.csv").read_text()
    wrong = tmp_path / "reference.csv"
    wrong.write_text(source.replace("0.5,20.45,", "0.5,20.55,"))
    assert wrong.read_text() != source
    root = prepare.fmu_replay(tmp_path / "fmu", runtime, build_dir / "EgoMotion.so",
                              reference_csv=wrong).parent
    sealed(runtime, root)

    proc = sil_bundle(runtime, "run", root, "-o", tmp_path / "evidence")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    result = summary(tmp_path / "evidence")
    assert result["verdict"] == "fail"
    (run,) = result["runs"]
    assert run["exit_code"] == 0
    assert run["determinism"]["verdict"] == "pass"
    assert run["comparisons"]["reference"]["verdict"] == "fail"


def test_a_behavioral_failure_fails_the_run(tmp_path, runtime, build_dir):
    root = prepare.library(tmp_path / "library", runtime,
                           build_dir / "speed_filter_defect.so").parent
    sealed(runtime, root)

    proc = sil_bundle(runtime, "run", root, "-o", tmp_path / "evidence")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    (run,) = summary(tmp_path / "evidence")["runs"]
    assert run["verdict"] == "fail"
    assert run["exit_code"] == 1
    assert run["determinism"] is None
    assert (tmp_path / "evidence" / "runs" / "library" / "run-1.log").read_text()


def test_a_moved_bundle_is_refused(library_bundle, runtime, tmp_path):
    moved = tmp_path / "moved"
    shutil.copytree(library_bundle, moved)

    proc = sil_bundle(runtime, "verify", moved)

    assert proc.returncode == 2
    assert "sealed at" in proc.stderr


def test_a_reachable_compiler_refuses_the_seal(tmp_path, runtime, library_binary):
    tools = tmp_path / "tools"
    tools.mkdir()
    compiler = tools / "cc"
    compiler.write_text("#!/bin/sh\n")
    compiler.chmod(0o755)
    root = tmp_path / "library"
    declaration = prepare.library(root, runtime, library_binary)
    document = json.loads(declaration.read_text())
    document["runtime"]["environment"]["PATH"] += f":{tools}"
    declaration.write_text(json.dumps(document))

    proc = sil_bundle(runtime, "seal", root)

    assert proc.returncode == 2
    assert f"excluded and present: executable cc at {compiler.resolve()}" in proc.stderr
    assert not (root / "bundle.lock.json").exists()


def test_seal_refuses_an_undeclared_file_and_an_undeclared_path(
        tmp_path, runtime, library_binary):
    root = tmp_path / "library"
    declaration = prepare.library(root, runtime, library_binary)
    document = json.loads(declaration.read_text())
    del document["artifacts"]["binding.py"]
    declaration.write_text(json.dumps(document))
    assert "file binding.py is in the bundle but not declared" in \
        sil_bundle(runtime, "seal", root).stderr

    outside = tmp_path / "outside"
    manifest = prepare.library(outside, runtime, library_binary, bundled=False)
    document = json.loads(manifest.read_text())
    document["dependencies"]["files"] = []
    manifest.write_text(json.dumps(document))
    proc = sil_bundle(runtime, "seal", outside)
    assert proc.returncode == 2
    assert f"library.json names {library_binary.resolve()}" in proc.stderr


def test_a_sealed_bundle_cannot_be_sealed_again(library_bundle, runtime):
    proc = sil_bundle(runtime, "seal", library_bundle)

    assert proc.returncode == 2
    assert "already sealed" in proc.stderr


@pytest.mark.parametrize("inside", [True, False])
def test_evidence_must_be_a_new_directory_outside_the_bundle(
        library_bundle, runtime, tmp_path, inside):
    if inside:
        evidence = library_bundle / "evidence"
    else:
        evidence = tmp_path / "used"
        evidence.mkdir()
        (evidence / "old.json").write_text("{}")

    proc = sil_bundle(runtime, "run", library_bundle, "-o", evidence)

    assert proc.returncode == 2
    assert "evidence directory" in proc.stderr
    assert not (library_bundle / "evidence").exists()
