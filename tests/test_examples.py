"""Installed onboarding workflows, with exported inputs and no checkout imports."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT
from test_staged_install import installed_environment


def invoke(python: Path, work: Path, *args, native: Path | None = None):
    env = installed_environment(python)
    if native is not None:
        env["PATH"] = f"{python / 'bin'}{os.pathsep}{native / 'bin'}{os.pathsep}{env['PATH']}"
    return subprocess.run(list(map(str, args)), cwd=work, env=env,
                          capture_output=True, text=True, timeout=60)


def checked(python: Path, work: Path, *args, native: Path | None = None):
    proc = invoke(python, work, *args, native=native)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


@pytest.fixture
def exported(installed_python: Path, tmp_path: Path) -> Path:
    checked(installed_python, tmp_path, installed_python / "bin" / "sil",
            "examples", "--dir", "examples")
    return tmp_path / "examples"


def test_wheel_ships_sources_bindings_and_licenses(wheel_path: Path):
    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        topics = json.loads(wheel.read("sil/_examples/topics.json"))
        for directory in topics:
            source = ROOT / "examples" / directory
            for path in source.rglob("*"):
                if path.is_file() and path.suffix in {
                        ".py", ".c", ".h", ".csv", ".json", ".xml", ".sh", ".txt"}:
                    member = f"sil/_examples/{directory}/{path.relative_to(source).as_posix()}"
                    assert wheel.read(member) == path.read_bytes()
        assert wheel.read("sil/_examples/adas-reference/LICENSE") == (ROOT / "LICENSE").read_bytes()
    assert not any("/__pycache__/" in n or n.endswith((".so", ".dylib", ".fmu"))
                   for n in names if n.startswith("sil/_examples/"))


def test_installed_examples_are_discoverable(installed_python: Path, tmp_path: Path):
    sil = installed_python / "bin" / "sil"
    listing = checked(installed_python, tmp_path, sil, "examples")
    assert "library" in listing.stdout and "bundle" in listing.stdout
    location = checked(installed_python, tmp_path, sil, "examples", "--path")
    root = Path(location.stdout.strip())
    assert root.is_relative_to(installed_python)
    assert (root / "library" / "adapter.py").is_file()
    report = checked(installed_python, tmp_path, sil, "docs", "recording-queries")
    assert report.stdout.startswith("# Bounded Recording queries\n")


def test_copy_refuses_existing_edits_without_writing(exported: Path, installed_python: Path):
    marker = exported / "library" / "binding.py"
    marker.write_text("user binding\n")
    before = {p.relative_to(exported): p.read_bytes()
              for p in exported.rglob("*") if p.is_file()}
    result = invoke(installed_python, exported.parent,
                    installed_python / "bin" / "sil", "examples", "--dir", exported)
    assert result.returncode == 2
    assert before == {p.relative_to(exported): p.read_bytes()
                      for p in exported.rglob("*") if p.is_file()}


def test_copy_accepts_empty_directory_and_refuses_installed_destination(
        installed_python: Path, tmp_path: Path):
    sil = installed_python / "bin" / "sil"
    destination = tmp_path / "examples"
    destination.mkdir()
    checked(installed_python, tmp_path, sil, "examples", "--dir", destination)
    root = Path(checked(installed_python, tmp_path, sil,
                        "examples", "--path").stdout.strip())
    result = invoke(installed_python, tmp_path, sil, "examples", "--dir", root / "copy")
    assert result.returncode == 2
    assert not (root / "copy").exists()


def test_unstamped_checkout_reports_missing_packaged_examples(tmp_path: Path):
    proc = subprocess.run([sys.executable, "-m", "sil.example_files"], cwd=tmp_path,
                          capture_output=True, text=True)
    assert proc.returncode == 1
    assert "a release wheel or the container image" in proc.stderr
    assert not list(tmp_path.iterdir())


def test_exported_fmu_manifest_accepts_a_supplied_archive(
        exported: Path, installed_python: Path, staged_prefix: Path):
    work = exported.parent
    archive = work / "supplier.fmu"
    shutil.copyfile(ROOT / "tests/fixtures/reference-fmus/3.0/Feedthrough.fmu", archive)
    python, sil = installed_python / "bin" / "python", installed_python / "bin" / "sil"
    checked(installed_python, work, python, exported / "fmu" / "manifest.py",
            "fmu.json", "--fmu", archive)
    assert str(ROOT) not in (work / "fmu.json").read_text()
    checked(installed_python, work, sil, "check", "fmu.json",
            "--participant-timeout-ms", "5000", native=staged_prefix)


@pytest.mark.parametrize("kind,source,mapping,script,channel,field,expected", [
    ("simple", "speed_filter", "mapping", "manifest", "filter.fast", "filtered_speed_mps", 8 / 3),
    ("ports", "gap_monitor", "gap_mapping", "gap_manifest", "monitor.gap", "time_gap_s", 2.5),
])
def test_exported_library_bindings_run_deterministically(
        exported, installed_python, staged_prefix, kind, source, mapping, script,
        channel, field, expected):
    work = exported.parent
    sil, python = installed_python / "bin" / "sil", installed_python / "bin" / "python"
    library = work / f"{kind}.so"
    checked(installed_python, work, "cc", "-shared", "-fPIC", "-O2", "-o", library,
            exported / "library" / f"{source}.c")
    signals = "gap_signals" if kind == "ports" else "signals"
    checked(installed_python, work, sil, "recording", "csv",
            exported / "library" / f"{mapping}.json",
            exported / "library" / f"{signals}.csv", "-o", "input.mcap")
    checked(installed_python, work, python, exported / "library" / f"{script}.py",
            "run.json", "--library", library, "--recording", work / "input.mcap")
    assert str(ROOT) not in (work / "run.json").read_text()
    checked(installed_python, work, sil, "check", "run.json", "--keep", "recordings",
            "--participant-timeout-ms", "5000", native=staged_prefix)
    rows = checked(installed_python, work, sil, "recording", "show", "recordings/run1.mcap",
                   "--channel", channel, "--limit", "1", "--json")
    assert json.loads(rows.stdout)["fields"][field] == pytest.approx(expected)


@pytest.mark.parametrize("kind", ["library", "fmu-replay", "coupling"])
def test_exported_preparation_seals_and_runs_each_bundle(
        exported: Path, installed_python: Path, staged_prefix: Path, kind: str):
    work = exported.parent
    sil, python = installed_python / "bin" / "sil", installed_python / "bin" / "python"
    if kind == "coupling":
        archive = work / "supplier.fmu"
        shutil.copyfile(ROOT / "tests/fixtures/reference-fmus/3.0/Feedthrough.fmu", archive)
        target = ["--fmu", archive, "--runner", staged_prefix / "bin" / "sil-run"]
    else:
        source = exported / ("library/speed_filter.c" if kind == "library"
                             else "fmu-replay/ego_motion.c")
        binary = work / "target.so"
        checked(installed_python, work, "cc", "-shared", "-fPIC", "-O2", "-o", binary, source)
        target = ["--library" if kind == "library" else "--fmu-binary", binary]
    bundle = work / "case"
    checked(installed_python, work, python, exported / "bundle" / "prepare.py", kind, bundle,
            "--python-bin", installed_python / "bin", "--sil-bin", staged_prefix / "bin",
            *target)
    for manifest in bundle.glob("*.json"):
        assert str(ROOT) not in manifest.read_text()
    checked(installed_python, work, sil, "bundle", "seal", bundle)
    checked(installed_python, work, sil, "bundle", "run", bundle, "-o", "evidence")
    result = json.loads((work / "evidence" / "summary.json").read_text())
    assert result["verdict"] == "pass"
    assert all(r["determinism"]["verdict"] == "pass" for r in result["runs"])


def test_exported_native_demo_and_fmu_packager_use_installed_dependencies(
        exported: Path, installed_python: Path, staged_prefix: Path):
    work = exported.parent
    checked(installed_python, work, "sh", exported / "adas-reference" / "run.sh",
            work / "native", native=staged_prefix)
    checked(installed_python, work, installed_python / "bin" / "python",
            exported / "adas-reference" / "fmu" / "package.py", work / "fmu")
    with zipfile.ZipFile(work / "fmu" / "AdasReference.fmu") as fmu:
        assert fmu.read("documentation/licenses/LICENSE-SiL.txt") == (ROOT / "LICENSE").read_bytes()
