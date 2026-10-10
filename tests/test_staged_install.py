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


def installed_environment(venv: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("LD_PRELOAD", None)
    env.pop("DYLD_INSERT_LIBRARIES", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")
    return env


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
        [str(installed_python / "bin" / "python"), "-m",
         "sil.examples.acc.manifest", str(manifest)],
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
        [str(installed_python / "bin" / "python"), "-m",
         "sil.examples.acc.manifest", str(manifest)],
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
    assert (installed_python / "bin" / "sil").is_file()
    assert not list((installed_python / "bin").glob("sil-*"))
    assert str(ROOT) not in origin.stdout
    assert str(ROOT) not in manifest.read_text()

    footprint = subprocess.run(
        [str(installed_python / "bin" / "sil"), "footprint", str(manifest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert footprint.returncode == 0, footprint.stderr
    assert str(ROOT) not in footprint.stdout

    participant = subprocess.run(
        [str(installed_python / "bin" / "python"), "-m", "sil.participant",
         "sil.examples.acc.safety:MinimumGapKPI"],
        cwd=tmp_path,
        env=env,
        input=PARTICIPANT_PROTOCOL,
        capture_output=True,
        text=True,
    )
    assert participant.returncode == 0, participant.stderr
    assert participant_operations(participant.stdout) == ["ready", "step_done"]


def test_schema_import_without_the_dwarf_extra_names_the_extra(
    installed_python: Path, tmp_path: Path,
):
    """The plain wheel does not install pyelftools (issue #253)."""
    proc = subprocess.run(
        [str(installed_python / "bin" / "sil"), "schema", "import", "types.o",
         "--type", "A=a.A", "-o", "schemas.json",
         "--layout-check", "layout_check.h"],
        cwd=tmp_path,
        env=installed_environment(installed_python),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "pip install 'sil[dwarf]'" in proc.stderr
    assert list(tmp_path.iterdir()) == []


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
        str(installed_python / "bin" / "python"), "-m",
        "sil.examples.acc.manifest", str(installed_manifest),
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
    build = [str(installed_python / "bin" / "python"), "-m",
             "sil.examples.acc.manifest", str(manifest)]
    if delayed_sensing:
        build.append("--delayed-sensing")
    env = installed_environment(installed_python)
    built = subprocess.run(
        build, cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    check = subprocess.run(
        [str(installed_python / "bin" / "sil"), "check", str(manifest),
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
        run("sil", "recording", "csv", str(example / "mapping.json"), str(example / "signals.csv"),
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
    # As the published commands run it: a hung library fails the Run.
    deadline = ("--participant-timeout-ms", "10000")
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
    run("sil", "recording", "csv", str(example / "mapping.json"), str(example / "signals.csv"),
        "-o", "signals.mcap", "--receipt", "signals.receipt.json")

    runs = []
    for attempt in ("1", "2"):
        run("python", str(example / "manifest.py"), f"library-{attempt}.json",
            "--recording", "signals.mcap", "--library", "speed_filter.so")
        run("sil-run", f"library-{attempt}.json", "-o", f"run-{attempt}.mcap",
            *deadline)
        runs.append((tmp_path / f"run-{attempt}.mcap").read_bytes())
    assert (tmp_path / "library-1.json").read_bytes() == (
        tmp_path / "library-2.json").read_bytes()
    assert runs[0] == runs[1]

    run("python", str(example / "manifest.py"), "defect.json",
        "--recording", "signals.mcap", "--library", "speed_filter_defect.so")
    failed = run("sil-run", "defect.json", "-o", "defect.mcap", *deadline,
                 code=1)
    assert "filter.fast at t=0 ns: filtered_speed_mps 4.0 differs" in (
        failed.stderr)
    assert not list(tmp_path.glob(".sil-run-*"))


def test_installed_fmu_replay_is_authored_run_and_compared(
    installed_python: Path, staged_prefix: Path, tmp_path: Path,
):
    """Issue #186's command sequence: the example FMU built and packaged,
    its input converted, the Run authored twice and run twice, and the
    outputs compared with the independent reference — SiL only from the
    installed wheel and prefix."""
    example = ROOT / "examples" / "fmu-replay"
    env = installed_environment(installed_python)
    env["PATH"] = str(staged_prefix / "bin") + os.pathsep + env["PATH"]

    def run(*command: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc

    run("cc", "-shared", "-fPIC", "-O2", "-o", "EgoMotion.so",
        str(example / "ego_motion.c"))
    run("python", str(example / "package.py"), "EgoMotion.so",
        "-o", "EgoMotion.fmu")
    run("sil", "recording", "csv", str(example / "mapping.json"), str(example / "recorded.csv"),
        "-o", "recorded.mcap", "--receipt", "recorded.receipt.json")
    run("sil", "recording", "csv", str(example / "reference-mapping.json"),
        str(example / "reference.csv"), "-o", "reference.mcap")

    runs = []
    for attempt in ("1", "2"):
        run("sil", "fmi", "replay", str(example / "authoring.json"), "EgoMotion.fmu",
            "--recording", "recorded.mcap", "-o", f"fmu-replay-{attempt}.json",
            "--receipt", f"authoring-{attempt}.json")
        run("sil-run", f"fmu-replay-{attempt}.json", "-o", f"run-{attempt}.mcap")
        runs.append((tmp_path / f"run-{attempt}.mcap").read_bytes())
    assert (tmp_path / "fmu-replay-1.json").read_bytes() == (
        tmp_path / "fmu-replay-2.json").read_bytes()
    assert runs[0] == runs[1]
    report = json.loads(run(
        "sil", "compare", str(example / "contract.json"), "run-1.mcap",
        "reference.mcap", "--json").stdout)
    assert report["verdict"] == "pass"
    assert report["channels"]["ego.motion"]["checked"] == 10
    assert not list(tmp_path.glob(".sil-run-*"))


def test_installed_numeric_fmu_replay_round_trips_each_type(
    installed_python: Path, staged_prefix: Path, tmp_path: Path,
):
    """Issue #189: Float32, Int32, UInt32 and UInt64 recorded into
    `Feedthrough` and back, inspected, authored and run twice with identical
    Recording bytes, and compared with the hand-written reference — SiL only
    from the installed wheel and prefix."""
    example = ROOT / "examples" / "fmu-numeric"
    fmu = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
    env = installed_environment(installed_python)
    env["PATH"] = str(staged_prefix / "bin") + os.pathsep + env["PATH"]

    def run(*command: str, code: int = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == code, proc.stdout + proc.stderr
        return proc

    authoring = json.loads((example / "authoring.json").read_text())
    mapping = {
        "sil_fmi_mapping": 1, "schemas": authoring["schemas"],
        "channels": {name: {"schema": c["schema"], "direction": c["direction"]}
                     for name, c in authoring["channels"].items()},
        "bind": [f"{b['channel']}:{b['field']}={b['variable']}"
                 for b in authoring["bind"]],
    }
    (tmp_path / "mapping.json").write_text(json.dumps(mapping))
    report = json.loads(run("sil", "fmi", "inspect", str(fmu), "--json",
                            "--mapping", "mapping.json").stdout)
    assert report["mapping"]["accepted"] is True
    narrow = json.loads(json.dumps(mapping))
    narrow["schemas"]["numeric.Sensor"]["fields"][3]["type"] = "u32"
    (tmp_path / "narrow.json").write_text(json.dumps(narrow))
    rejected = json.loads(run("sil", "fmi", "inspect", str(fmu), "--json",
                              "--mapping", "narrow.json", code=3).stdout)
    assert "'u64' scalar" in rejected["mapping"]["rejection"]

    run("sil", "recording", "csv", str(example / "mapping.json"), str(example / "recorded.csv"),
        "-o", "recorded.mcap")
    run("sil", "recording", "csv", str(example / "reference-mapping.json"),
        str(example / "reference.csv"), "-o", "reference.mcap")
    runs = []
    for attempt in ("1", "2"):
        run("sil", "fmi", "replay", str(example / "authoring.json"), str(fmu),
            "--recording", "recorded.mcap", "-o", f"numeric-{attempt}.json",
            "--receipt", f"authoring-{attempt}.json")
        run("sil-run", f"numeric-{attempt}.json", "-o", f"run-{attempt}.mcap")
        runs.append((tmp_path / f"run-{attempt}.mcap").read_bytes())
    assert runs[0] == runs[1]
    compared = json.loads(run(
        "sil", "compare", str(example / "contract.json"), "run-1.mcap",
        "reference.mcap", "--json").stdout)
    assert compared["verdict"] == "pass"
    assert compared["channels"]["sensor.out"]["checked"] == 10
    assert not list(tmp_path.glob(".sil-run-*"))


def test_installed_array_fmu_replay_and_coupling_round_trip(
    installed_python: Path, staged_prefix: Path, build_dir: Path,
    tmp_path: Path,
):
    """Issue #190: `[8]`, `[3]` and `[2,3]` arrays recorded into the array
    fixture FMU and back, inspected, authored, run twice inline and twice
    over shared memory with identical Recording bytes, compared element by
    element with the hand-stated reference, refused against a transposed
    one; and a coupled matrix loop authored, run twice and checked against
    the fixture's rule — SiL only from the installed wheel and prefix."""
    import struct

    from array_fixture import (
        MODEL_IDENTIFIER,
        array_fmu,
        expected_matrix,
        nested,
        row_major,
    )
    from sil.fmi import library_suffix, platform_directory
    from sil.recording import read_records

    example = ROOT / "examples" / "fmu-array"
    binary = build_dir / f"{MODEL_IDENTIFIER}{library_suffix()}"
    fmu = array_fmu(tmp_path / "ArrayEcho.fmu", binary, platform_directory())
    env = installed_environment(installed_python)
    env["PATH"] = str(staged_prefix / "bin") + os.pathsep + env["PATH"]

    def run(*command: str, code: int = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == code, proc.stdout + proc.stderr
        return proc

    authoring = json.loads((example / "authoring.json").read_text())
    mapping = {
        "sil_fmi_mapping": 1, "schemas": authoring["schemas"],
        "channels": {name: {"schema": c["schema"], "direction": c["direction"]}
                     for name, c in authoring["channels"].items()},
        "bind": [f"{b['channel']}:{b['field']}={b['variable']}"
                 for b in authoring["bind"]],
        "start": [f"{s['variable']}={s['value']}" for s in authoring["start"]],
    }
    (tmp_path / "mapping.json").write_text(json.dumps(mapping))
    report = json.loads(run("sil", "fmi", "inspect", str(fmu), "--json",
                            "--mapping", "mapping.json").stdout)
    assert report["mapping"]["accepted"] is True
    matrix = next(v for v in report["variables"] if v["name"] == "matrix_in")
    assert (matrix["dimensions"], matrix["value_count"]) == (
        [{"start": 2}, {"start": 3}], 6)

    run("sil", "recording", "csv", str(example / "mapping.json"), str(example / "recorded.csv"),
        "-o", "recorded.mcap")
    run("sil", "recording", "csv", str(example / "reference-mapping.json"),
        str(example / "reference.csv"), "-o", "reference.mcap")
    run("sil", "fmi", "replay", str(example / "authoring.json"), str(fmu),
        "--recording", "recorded.mcap", "-o", "inline.json",
        "--receipt", "receipt.json")
    shm = json.loads((tmp_path / "inline.json").read_text())
    for channel in shm["channels"].values():
        channel.update(transport="shm", slots=2)
    (tmp_path / "shm.json").write_text(json.dumps(shm))
    for transport in ("inline", "shm"):
        recordings = []
        for attempt in ("1", "2"):
            out = f"{transport}-{attempt}.mcap"
            run("sil-run", f"{transport}.json", "-o", out)
            recordings.append((tmp_path / out).read_bytes())
        assert recordings[0] == recordings[1]
        compared = json.loads(run(
            "sil", "compare", str(example / "contract.json"),
            f"{transport}-1.mcap", "reference.mcap", "--json").stdout)
        assert compared["verdict"] == "pass", compared["first_divergence"]
        assert compared["channels"]["sensor.out"]["checked"] == 4

    transposed = json.loads((example / "reference-mapping.json").read_text())
    transposed["channels"][0]["fields"]["matrix"]["columns"] = [
        f"matrix_{i}_{j}" for j in range(3) for i in range(2)]
    (tmp_path / "transposed.json").write_text(json.dumps(transposed))
    run("sil", "recording", "csv", "transposed.json", str(example / "reference.csv"),
        "-o", "transposed.mcap")
    failed = json.loads(run(
        "sil", "compare", str(example / "contract.json"), "inline-1.mcap",
        "transposed.mcap", "--json", code=1).stdout)
    assert failed["first_divergence"]["field"] == "matrix"

    coupled = []
    for attempt in ("1", "2"):
        run("sil", "fmi", "couple", str(example / "coupling.json"),
            "--fmu", "left", str(fmu), "--fmu", "right", str(fmu),
            "-o", "coupled.json")
        run("sil-run", "coupled.json", "-o", f"coupled-{attempt}.mcap")
        coupled.append((tmp_path / f"coupled-{attempt}.mcap").read_bytes())
    assert coupled[0] == coupled[1]
    # Each FMU holds its matrix input at 0 until the first delivery, and each
    # delivery is the other FMU's previous Message.
    loop = json.loads((example / "coupling.json").read_text())
    bias = {name: nested([float(v) for v in fmu["start"][0]["value"].split()])
            for name, fmu in loop["fmus"].items()}
    values = {"left.matrix": [], "right.matrix": []}
    for topic, _, data in read_records(tmp_path / "coupled-1.mcap"):
        values[topic].append(list(struct.unpack("<6d", data)))
    held = {"left": [[0.0] * 3] * 2, "right": [[0.0] * 3] * 2}
    for step in range(4):
        for name in ("left", "right"):
            assert values[f"{name}.matrix"][step] == row_major(
                expected_matrix(held[name], bias[name]))
        held = {"left": nested(values["right.matrix"][step]),
                "right": nested(values["left.matrix"][step])}
    assert not list(tmp_path.glob(".sil-run-*"))


def test_installed_window_replays_into_the_library_after_a_warm_up(
    installed_python: Path, staged_prefix: Path, tmp_path: Path,
):
    """Issue #185's command sequence: a selected window of the history, run
    after a warm-up, agrees with the full history over the evaluation
    interval; the same interval without a warm-up does not."""
    example = ROOT / "examples" / "library"
    deadline = ("--participant-timeout-ms", "10000")
    env = installed_environment(installed_python)
    env["PATH"] = str(staged_prefix / "bin") + os.pathsep + env["PATH"]

    def run(*command: str, code: int = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == code, proc.stdout + proc.stderr
        return proc

    run("cc", "-shared", "-fPIC", "-O2", "-o", "speed_filter.so",
        str(example / "speed_filter.c"))
    run("sil", "recording", "csv", str(example / "mapping.json"), str(example / "history.csv"),
        "-o", "history.mcap", "--receipt", "history.receipt.json")
    run("python", str(example / "manifest.py"), "full.json",
        "--recording", "history.mcap", "--library", "speed_filter.so",
        "--duration-ns", "2500000000")
    run("sil-run", "full.json", "-o", "full.mcap", *deadline)

    recordings, runs = [], []
    for attempt in ("1", "2"):
        run("sil", "recording", "window", str(example / "window.json"), "history.mcap",
            "-o", "window.mcap", "--receipt", "window.receipt.json")
        recordings.append((tmp_path / "window.mcap").read_bytes())
        run("python", str(example / "manifest.py"), "windowed.json",
            "--recording", "window.mcap", "--library", "speed_filter.so",
            "--duration-ns", "2000000000")
        run("sil-run", "windowed.json", "-o", f"window-{attempt}.mcap",
            *deadline)
        runs.append((tmp_path / f"window-{attempt}.mcap").read_bytes())
    assert recordings[0] == recordings[1]
    assert runs[0] == runs[1]
    run("python", str(example / "window_contract.py"), "window.receipt.json",
        "-o", "contract.json")
    run("sil", "compare", "contract.json", "window-1.mcap", "full.mcap")

    run("sil", "recording", "window", str(example / "window-no-warm-up.json"), "history.mcap",
        "-o", "cold.mcap", "--receipt", "cold.receipt.json")
    run("python", str(example / "manifest.py"), "cold.json",
        "--recording", "cold.mcap", "--library", "speed_filter.so",
        "--duration-ns", "1000000000")
    run("sil-run", "cold.json", "-o", "cold-run.mcap", *deadline)
    run("python", str(example / "window_contract.py"), "cold.receipt.json",
        "-o", "cold-contract.json")
    failed = run("sil", "compare", "cold-contract.json", "cold-run.mcap",
                 "full.mcap", code=1)
    assert "fail" in failed.stdout


def test_installed_fmu_inspection_needs_only_the_wheel(
    installed_python: Path, tmp_path: Path,
):
    """Issue #181's command, with only the installed wheel on PATH."""
    env = installed_environment(installed_python)
    fmu = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
    proc = subprocess.run(
        ["sil", "fmi", "inspect", str(fmu), "--json",
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
    independent ACC trace converted by sil recording csv, then compared."""
    env = installed_environment(installed_python)
    example = ROOT / "examples" / "compare"
    converted = subprocess.run(
        ["sil", "recording", "csv", str(example / "reference-mapping.json"),
         str(example / "plant-accelerate.reference.csv"),
         "-o", "reference.mcap"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert converted.returncode == 0, converted.stderr
    proc = subprocess.run(
        ["sil", "compare", str(example / "contract.json"),
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
