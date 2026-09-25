"""Consumer-facing checks for a staged SiL installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACC_MANIFEST_HASHES = {
    "nominal": "224f7991e555c12418e996f9d48b816e3e35224da61e68876147d8d357073e59",
    "delayed": "7a9ffa5122c9a7859da23ae6d7a09ebd8ecfd85fea35e07bdca6a2a2b929a954",
}
PARTICIPANT_PROTOCOL = (
    '{"op":"init","name":"test","protocol":1,"channels":{},'
    '"schemas":{}}\n'
    '{"op":"step","t":0,"dt":10000000,"in":[]}\n'
    '{"op":"shutdown"}\n'
)


def participant_operations(stdout: str) -> list[str]:
    return [json.loads(line)["op"] for line in stdout.splitlines()]


def uv_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(ROOT / "build" / "uv-cache"))
    return env


@pytest.fixture(scope="session")
def wheel_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheel_dir = tmp_path_factory.mktemp("sil-wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir),
         str(ROOT / "python")],
        check=True, env=uv_environment(),
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
    env = uv_environment()
    subprocess.run(
        ["uv", "venv", "-p", sys.executable, str(venv)],
        check=True, env=env,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "-p", str(venv / "bin" / "python"),
         str(wheel_path)],
        check=True, env=env,
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


@pytest.fixture(scope="session")
def custom_staged_prefix(
    build_dir: Path, tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Install a second runner with a slash-terminated custom bindir."""
    custom_build = tmp_path_factory.mktemp("sil-custom-build")
    prefix = tmp_path_factory.mktemp("sil-custom-prefix")
    deps = build_dir / "_deps"
    subprocess.run(
        [
            "cmake", "-S", str(ROOT), "-B", str(custom_build),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_BINDIR=custom/bin/",
            f"-DFETCHCONTENT_SOURCE_DIR_NLOHMANN_JSON={deps / 'nlohmann_json-src'}",
            f"-DFETCHCONTENT_SOURCE_DIR_MCAP={deps / 'mcap-src'}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["cmake", "--build", str(custom_build),
         "--target", "sil-run", "sil_clock_shim", "-j"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["cmake", "--install", str(custom_build), "--prefix", str(prefix)],
        check=True,
        capture_output=True,
        text=True,
    )
    return prefix


def test_cmake_stages_the_production_runtime_and_public_headers(
    staged_prefix: Path,
):
    assert (staged_prefix / "bin" / "sil-run").is_file()
    assert (staged_prefix / "bin" / "silschema").is_file()
    assert (staged_prefix / "include" / "sil" / "participant.h").is_file()
    assert (staged_prefix / "include" / "sil" / "arena.h").is_file()
    assert (staged_prefix / "include" / "sil" / "clock_region.h").is_file()

    if sys.platform == "darwin":
        shim = staged_prefix / "lib" / "libsil_clock_shim.dylib"
    elif sys.platform.startswith("linux"):
        shim = staged_prefix / "lib" / "libsil_clock_shim.so"
    else:
        pytest.skip("staged runtime layout is only exercised on POSIX hosts")
    assert shim.is_file()
    assert not (staged_prefix / "bin" / "sil-run-instrumented").exists()
    metadata = json.loads((staged_prefix / "share" / "sil" / "release.json").read_text())
    assert metadata["version"] == "0.1.0"
    assert metadata["license"] == "Apache-2.0"
    assert metadata["source_repository"] == "https://github.com/Stevie1704/sil"


def test_cmake_stages_the_license_the_release_archives(staged_prefix: Path):
    """The native-development archive is made from this prefix (issue #122)."""
    licenses = staged_prefix / "share" / "licenses" / "sil"
    assert "Apache License" in (licenses / "LICENSE").read_text()
    assert "SPDX-License-Identifier: Apache-2.0" in (licenses / "NOTICE").read_text()
    assert (licenses / "THIRD-PARTY-NOTICES.md").is_file()
    # Both are header-only and compiled into sil-run, so their notices travel
    # with the binary the archive carries.
    assert (licenses / "mcap" / "LICENSE").is_file()
    assert (licenses / "nlohmann-json" / "LICENSE.MIT").is_file()


@pytest.mark.skipif(
    not (sys.platform == "darwin" or sys.platform.startswith("linux")),
    reason="staged runtime layout is only exercised on POSIX hosts",
)
def test_custom_bindir_with_trailing_slash_resolves_the_staged_shim(
    custom_staged_prefix: Path, installed_python: Path, tmp_path: Path,
):
    assert (custom_staged_prefix / "custom" / "bin" / "sil-run").is_file()
    if sys.platform == "darwin":
        shim = custom_staged_prefix / "lib" / "libsil_clock_shim.dylib"
    else:
        shim = custom_staged_prefix / "lib" / "libsil_clock_shim.so"
    assert shim.is_file()

    manifest = tmp_path / "acc.json"
    recording = tmp_path / "acc.mcap"
    env = installed_environment(installed_python)
    built = subprocess.run(
        [str(installed_python / "bin" / "sil-acc"), str(manifest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    run = subprocess.run(
        [str(custom_staged_prefix / "custom" / "bin" / "sil-run"),
         str(manifest), "-o", str(recording)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert built.returncode == 0, built.stderr
    assert run.returncode == 0, run.stderr
    assert recording.is_file()


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


@pytest.mark.parametrize("delayed_sensing", [False, True])
def test_acc_manifest_source_tree_hash_is_stable(
    tmp_path: Path, delayed_sensing: bool,
):
    variant = "delayed" if delayed_sensing else "nominal"
    source_manifest = tmp_path / f"source-{variant}.json"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "python" / "src"))
    source_build = [
        sys.executable, "-m", "sil.examples.acc.manifest",
        str(source_manifest),
    ]
    if delayed_sensing:
        source_build.append("--delayed-sensing")
    source = subprocess.run(
        source_build,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert source.returncode == 0, source.stderr
    assert source_manifest.is_file()
    assert source.stdout.strip() == ACC_MANIFEST_HASHES[variant]


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
    assert proc.stdout.strip() == ACC_MANIFEST_HASHES["nominal"]
    assert manifest.is_file()
    for entrypoint in ("sil-acc", "sil-check", "sil-compare", "sil-csv",
                       "sil-fmi-inspect", "sil-footprint", "sil-participant"):
        assert (installed_python / "bin" / entrypoint).is_file()
    assert str(ROOT) not in origin.stdout
    assert str(ROOT) not in manifest.read_text()

    footprint = subprocess.run(
        [str(installed_python / "bin" / "sil-footprint"), str(manifest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert footprint.returncode == 0, footprint.stderr
    assert str(ROOT) not in footprint.stdout

    participant = subprocess.run(
        [str(installed_python / "bin" / "sil-participant"),
         "sil.examples.acc.safety:MinimumGapKPI"],
        cwd=tmp_path,
        env=env,
        input=PARTICIPANT_PROTOCOL,
        capture_output=True,
        text=True,
    )
    assert participant.returncode == 0, participant.stderr
    assert participant_operations(participant.stdout) == ["ready", "step_done"]


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
        sys.executable, "-m", "sil.examples.acc.manifest",
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


def test_installed_csv_conversion_drives_the_staged_replay(
    installed_python: Path, staged_prefix: Path, tmp_path: Path,
):
    """Issue #179's command sequence, with only installed tools on PATH."""
    from test_example_csv import CONSUMER_VIEW

    example = ROOT / "examples" / "csv"
    env = installed_environment(installed_python)
    env["PATH"] = str(staged_prefix / "bin") + os.pathsep + env["PATH"]

    def run(*command: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc

    recordings, runs = [], []
    for attempt in ("1", "2"):
        run("sil-csv", str(example / "mapping.json"), str(example / "signals.csv"),
            "-o", "signals.mcap", "--receipt", f"receipt-{attempt}.json")
        recordings.append((tmp_path / "signals.mcap").read_bytes())
        run("python", str(example / "manifest.py"), f"replay-{attempt}.json",
            "--recording", "signals.mcap")
        run("sil-run", f"replay-{attempt}.json", "-o", f"run-{attempt}.mcap")
        runs.append((tmp_path / f"run-{attempt}.mcap").read_bytes())

    assert recordings[0] == recordings[1]
    assert (tmp_path / "receipt-1.json").read_bytes() == (
        tmp_path / "receipt-2.json").read_bytes()
    assert (tmp_path / "replay-1.json").read_bytes() == (
        tmp_path / "replay-2.json").read_bytes()
    assert runs[0] == runs[1]

    from sil import schema
    from sil.recording import read_records

    seen = schema.load(
        json.loads((tmp_path / "replay-1.json").read_text())["schemas"]
    )["csv.Seen"]
    assert [(t, seen.unpack(data))
            for topic, t, data in read_records(tmp_path / "run-1.mcap")
            if topic == "csv.seen"] == CONSUMER_VIEW


def test_installed_library_example_replays_into_an_adopter_build(
    installed_python: Path, staged_prefix: Path, tmp_path: Path,
):
    """Issue #184's command sequence: the library built by the adopter's own
    compiler, SiL only from the installed wheel and prefix."""
    example = ROOT / "examples" / "library"
    env = installed_environment(installed_python)
    env["PATH"] = str(staged_prefix / "bin") + os.pathsep + env["PATH"]

    def run(*command: str, code: int = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == code, proc.stderr
        return proc

    run("cc", "-shared", "-fPIC", "-O2", "-o", "speed_filter.so",
        str(example / "speed_filter.c"))
    run("cc", "-shared", "-fPIC", "-O2", "-DSPEED_FILTER_DEFECT",
        "-o", "speed_filter_defect.so", str(example / "speed_filter.c"))
    run("sil-csv", str(example / "mapping.json"), str(example / "signals.csv"),
        "-o", "signals.mcap", "--receipt", "signals.receipt.json")

    runs = []
    for attempt in ("1", "2"):
        run("python", str(example / "manifest.py"), f"library-{attempt}.json",
            "--recording", "signals.mcap", "--library", "speed_filter.so")
        run("sil-run", f"library-{attempt}.json", "-o", f"run-{attempt}.mcap")
        runs.append((tmp_path / f"run-{attempt}.mcap").read_bytes())
    assert (tmp_path / "library-1.json").read_bytes() == (
        tmp_path / "library-2.json").read_bytes()
    assert runs[0] == runs[1]

    run("python", str(example / "manifest.py"), "defect.json",
        "--recording", "signals.mcap", "--library", "speed_filter_defect.so")
    failed = run("sil-run", "defect.json", "-o", "defect.mcap", code=1)
    assert "filter.fast at t=0 ns: filtered_speed_mps 4.0 differs" in (
        failed.stderr)
    assert not list(tmp_path.glob(".sil-run-*"))


def test_installed_fmu_inspection_needs_only_the_wheel(
    installed_python: Path, tmp_path: Path,
):
    """Issue #181's command, with only the installed wheel on PATH."""
    env = installed_environment(installed_python)
    fmu = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
    proc = subprocess.run(
        ["sil-fmi-inspect", str(fmu), "--json",
         "--mapping", str(ROOT / "examples" / "fmu" / "mapping.json")],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["verdict"] == "compatible"
    assert report["mapping"]["accepted"] is True
    assert list(tmp_path.iterdir()) == []


def test_installed_comparison_needs_only_the_wheel(
    installed_python: Path, tmp_path: Path,
):
    """Issue #182's command, with only the installed wheel on PATH: the
    independent ACC trace converted by sil-csv, then compared."""
    env = installed_environment(installed_python)
    example = ROOT / "examples" / "compare"
    converted = subprocess.run(
        ["sil-csv", str(example / "reference-mapping.json"),
         str(example / "plant-accelerate.reference.csv"),
         "-o", "reference.mcap"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert converted.returncode == 0, converted.stderr
    proc = subprocess.run(
        ["sil-compare", str(example / "contract.json"),
         str(ROOT / "proofs" / "acc-fmi" / "evidence" / "plant-accelerate-1.mcap"),
         "reference.mcap", "--json"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["verdict"] == "pass"
    assert report["channels"]["output0"]["checked"] == 10


INSTALLED_BOUNDED_RUN = '''
import sys
from sil.manifest import Manifest
from sil.testing import RunFailure, run_simulation

manifest = Manifest(duration_ns=10)
manifest.add_process("hung", command=[sys.executable, sys.argv[2], "step"],
                     step_period_ns=1)
try:
    run_simulation(manifest, runner=sys.argv[1], workdir=".",
                   participant_timeout_ms=100)
except RunFailure as failure:
    print(failure.exit_code)
    print(failure)
'''


def test_installed_pytest_helper_bounds_a_stalled_participant(
    installed_python: Path, staged_prefix: Path, tmp_path: Path,
):
    """Issue #183's helper, with only the installed wheel importable."""
    proc = subprocess.run(
        [str(installed_python / "bin" / "python"), "-c", INSTALLED_BOUNDED_RUN,
         str(staged_prefix / "bin" / "sil-run"),
         str(ROOT / "tests" / "participants" / "timeout.py")],
        cwd=tmp_path, env=installed_environment(installed_python),
        capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    exit_code, diagnostic = proc.stdout.split("\n", 1)
    assert exit_code == "1"
    assert "timeout" in diagnostic
    assert "virtual time 0 ns" in diagnostic


def test_participant_frontend_loads_a_package_module_spec(tmp_path: Path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "python" / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "sil.participant",
         "sil.examples.acc.safety:MinimumGapKPI"],
        cwd=tmp_path,
        env=env,
        input=PARTICIPANT_PROTOCOL,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert participant_operations(proc.stdout) == ["ready", "step_done"]
