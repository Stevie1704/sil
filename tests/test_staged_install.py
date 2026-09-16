"""Consumer-facing checks for a staged SiL installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def wheel_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheel_dir = tmp_path_factory.mktemp("sil-wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir),
         str(ROOT / "python")],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_dir.glob("sil-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture(scope="session")
def installed_python(
    wheel_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    venv = tmp_path_factory.mktemp("sil-venv")
    subprocess.run(
        ["uv", "venv", "-p", "/usr/bin/python3", str(venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "-p", str(venv / "bin" / "python"),
         str(wheel_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return venv


def installed_environment(venv: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("LD_PRELOAD", None)
    env.pop("DYLD_INSERT_LIBRARIES", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")
    return env


@pytest.fixture(scope="session")
def staged_prefix(build_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    prefix = tmp_path_factory.mktemp("sil-prefix")
    subprocess.run(
        ["cmake", "--install", str(build_dir), "--prefix", str(prefix)],
        check=True,
        capture_output=True,
        text=True,
    )
    return prefix


def test_cmake_stages_the_production_runtime_and_public_headers(
    staged_prefix: Path,
):
    assert (staged_prefix / "bin" / "sil-run").is_file()
    assert (staged_prefix / "include" / "sil" / "participant.h").is_file()
    assert (staged_prefix / "include" / "sil" / "clock_region.h").is_file()

    if sys.platform == "darwin":
        shim = staged_prefix / "lib" / "libsil_clock_shim.dylib"
    elif sys.platform.startswith("linux"):
        shim = staged_prefix / "lib" / "libsil_clock_shim.so"
    else:
        pytest.skip("staged runtime layout is only exercised on POSIX hosts")
    assert shim.is_file()
    assert not (staged_prefix / "bin" / "sil-run-instrumented").exists()


def test_acc_manifest_cli_uses_installable_participant_specs(tmp_path: Path):
    manifest = tmp_path / "acc.json"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "python" / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "sil.examples.acc.manifest", str(manifest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    document = json.loads(manifest.read_text())
    assert document["participants"]["plant"]["command"] == [
        "python3", "-m", "sil.participant", "sil.examples.acc.plant:Plant"
    ]
    assert document["participants"]["controller"]["command"] == [
        "python3", "-m", "sil.participant",
        "sil.examples.acc.controller:Controller"
    ]
    assert str(Path(__file__).parents[1]) not in manifest.read_text()


def test_acc_manifest_bytes_match_the_source_tree_development_setup(
    tmp_path: Path,
):
    source_manifest = tmp_path / "source.json"
    package_manifest = tmp_path / "package.json"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "python" / "src"))
    source = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "acc" / "manifest.py"),
         str(source_manifest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    package = subprocess.run(
        [sys.executable, "-m", "sil.examples.acc.manifest",
         str(package_manifest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert source.returncode == package.returncode == 0
    assert source_manifest.read_bytes() == package_manifest.read_bytes()
    assert source.stdout == package.stdout


def test_wheel_installs_acc_entrypoint_without_checkout_imports(
    installed_python: Path, tmp_path: Path,
):
    manifest = tmp_path / "installed-acc.json"
    env = installed_environment(installed_python)
    proc = subprocess.run(
        [str(installed_python / "bin" / "sil-acc"), str(manifest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    origin = subprocess.run(
        [str(installed_python / "bin" / "python"), "-c",
         "import sil; print(sil.__file__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert manifest.is_file()
    for entrypoint in ("sil-acc", "sil-check", "sil-footprint",
                       "sil-participant"):
        assert (installed_python / "bin" / entrypoint).is_file()
    assert str(ROOT) not in origin.stdout
    assert str(ROOT) not in manifest.read_text()


@pytest.mark.parametrize("delayed_sensing", [False, True])
def test_installed_acc_run_matches_source_tree_run(
    installed_python: Path, staged_prefix: Path, build_dir: Path, tmp_path: Path,
    delayed_sensing: bool,
):
    variant = "delayed" if delayed_sensing else "nominal"
    source_manifest = tmp_path / f"source-{variant}.json"
    installed_manifest = tmp_path / f"installed-{variant}.json"
    source_recording = tmp_path / f"source-{variant}.mcap"
    installed_recording = tmp_path / f"installed-{variant}.mcap"

    source_env = dict(os.environ, PYTHONPATH=str(ROOT / "python" / "src"))
    source_build = [
        sys.executable, str(ROOT / "examples" / "acc" / "manifest.py"),
        str(source_manifest),
    ]
    installed_build = [
        str(installed_python / "bin" / "sil-acc"), str(installed_manifest),
    ]
    if delayed_sensing:
        source_build.append("--delayed-sensing")
        installed_build.append("--delayed-sensing")

    source = subprocess.run(
        source_build, cwd=tmp_path, env=source_env,
        capture_output=True, text=True,
    )
    installed_env = installed_environment(installed_python)
    installed = subprocess.run(
        installed_build, cwd=tmp_path, env=installed_env,
        capture_output=True, text=True,
    )

    assert source.returncode == installed.returncode == 0
    assert source_manifest.read_bytes() == installed_manifest.read_bytes()
    assert source.stdout == installed.stdout
    assert str(ROOT) not in installed_manifest.read_text()

    source_run = subprocess.run(
        [str(build_dir / "sil-run"), str(source_manifest), "-o",
         str(source_recording)],
        cwd=tmp_path,
        env=source_env,
        capture_output=True,
        text=True,
    )
    installed_run = subprocess.run(
        [str(staged_prefix / "bin" / "sil-run"), str(installed_manifest),
         "-o", str(installed_recording)],
        cwd=tmp_path,
        env=installed_env,
        capture_output=True,
        text=True,
    )

    assert source_run.returncode == 0, source_run.stderr
    assert installed_run.returncode == 0, installed_run.stderr
    assert source_recording.read_bytes() == installed_recording.read_bytes()


@pytest.mark.parametrize("delayed_sensing", [False, True])
def test_installed_acc_determinism_check_uses_only_staged_inputs(
    installed_python: Path, staged_prefix: Path, tmp_path: Path,
    delayed_sensing: bool,
):
    manifest = tmp_path / "acc.json"
    build = [str(installed_python / "bin" / "sil-acc"), str(manifest)]
    if delayed_sensing:
        build.append("--delayed-sensing")
    env = installed_environment(installed_python)
    built = subprocess.run(
        build, cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    check = subprocess.run(
        [str(installed_python / "bin" / "sil-check"), str(manifest),
         "--runner", str(staged_prefix / "bin" / "sil-run")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert built.returncode == 0, built.stderr
    assert check.returncode == 0, check.stderr
    assert check.stdout.startswith("deterministic: ")


def test_participant_frontend_loads_a_package_module_spec(tmp_path: Path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "python" / "src"))
    protocol = (
        '{"op":"init","name":"test","protocol":1,"channels":{},'
        '"schemas":{}}\n'
        '{"op":"step","t":0,"dt":10000000,"in":[]}\n'
        '{"op":"shutdown"}\n'
    )
    proc = subprocess.run(
        [sys.executable, "-m", "sil.participant",
         "sil.examples.acc.safety:MinimumGapKPI"],
        cwd=tmp_path,
        env=env,
        input=protocol,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert [json.loads(line)["op"] for line in proc.stdout.splitlines()] == [
        "ready", "step_done"
    ]
