"""Black-box acceptance checks for the Linux Run container (issue #115)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from sil.recording import read_records
from sil.schema import load as load_schemas


PRODUCTION_IMAGE = os.environ.get("SIL_CONTAINER_IMAGE")
ACCEPTANCE_IMAGE = os.environ.get("SIL_CONTAINER_ACCEPTANCE_IMAGE")
NATIVE_RUNNER = os.environ.get("SIL_NATIVE_RUNNER")
PARTICIPANT_TIMEOUT_MS = "5000"

pytestmark = pytest.mark.skipif(
    not (PRODUCTION_IMAGE and ACCEPTANCE_IMAGE and NATIVE_RUNNER),
    reason=(
        "container acceptance needs SIL_CONTAINER_IMAGE, "
        "SIL_CONTAINER_ACCEPTANCE_IMAGE, and SIL_NATIVE_RUNNER"
    ),
)


def _docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )


def _container(
    image: str,
    workspace: Path,
    *args: str,
    entrypoint: str | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    command = [
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,src={workspace},dst={workspace}",
        "--workdir",
        str(workspace),
    ]
    if entrypoint is not None:
        command += ["--entrypoint", entrypoint]
    return _docker(*command, image, *args, timeout=timeout)


def _native_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("LD_PRELOAD", None)
    env["PYTHONNOUSERSITE"] = "1"
    python_bin = Path(NATIVE_RUNNER).resolve().parents[1] / "python" / "bin"
    env["PATH"] = str(python_bin) + os.pathsep + env.get("PATH", "")
    return env


def _run_native(
    workspace: Path, manifest: Path, recording: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(Path(NATIVE_RUNNER).resolve()),
            str(manifest),
            "--participant-timeout-ms",
            PARTICIPANT_TIMEOUT_MS,
            "-o",
            str(recording),
        ],
        cwd=workspace,
        env=_native_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_container(
    image: str, workspace: Path, manifest: Path, recording: Path
) -> subprocess.CompletedProcess[str]:
    return _container(
        image,
        workspace,
        str(manifest),
        "--participant-timeout-ms",
        PARTICIPANT_TIMEOUT_MS,
        "-o",
        str(recording),
    )


def _assert_hash_output(proc: subprocess.CompletedProcess[str], manifest: Path):
    expected = hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert proc.stdout == f"manifest_hash {expected}\n"


def _assert_deterministic_and_native_equivalent(
    image: str, workspace: Path, manifest: Path
):
    first = workspace / "container-1.mcap"
    second = workspace / "container-2.mcap"
    native = workspace / "native.mcap"

    first_run = _run_container(image, workspace, manifest, first)
    second_run = _run_container(image, workspace, manifest, second)
    native_run = _run_native(workspace, manifest, native)

    assert first_run.returncode == 0, first_run.stderr
    assert second_run.returncode == 0, second_run.stderr
    assert native_run.returncode == 0, native_run.stderr
    _assert_hash_output(first_run, manifest)
    assert first_run.stdout == second_run.stdout == native_run.stdout
    assert first.read_bytes() == second.read_bytes() == native.read_bytes()


def _assert_bounded_footprint(image: str, workspace: Path, manifest: Path):
    footprint = _container(
        image,
        workspace,
        str(manifest),
        entrypoint="sil-footprint",
    )
    assert footprint.returncode == 0, footprint.stderr
    assert "unbounded" not in footprint.stdout
    assert "declared payload total" in footprint.stdout


def test_production_image_contains_only_the_installed_runtime():
    inspected = _docker("image", "inspect", PRODUCTION_IMAGE)
    assert inspected.returncode == 0, inspected.stderr
    config = json.loads(inspected.stdout)[0]["Config"]
    assert config["User"] not in ("", "0", "0:0", "root")
    assert config["Entrypoint"] == ["sil-run"]

    probe = _docker(
        "run",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        PRODUCTION_IMAGE,
        "-ec",
        """
          test -x /usr/local/bin/sil-run
          test -f /usr/local/lib/libsil_clock_shim.so
          python3 -c 'import sil, mcap, lz4, zstandard'
          python3 -c 'import importlib.metadata as m; assert {n: m.version(n) for n in ("lz4", "mcap", "zstandard")} == {"lz4": "4.4.5", "mcap": "1.4.0", "zstandard": "0.25.0"}'
          ldd /usr/local/bin/sil-run | grep -q 'not found' && exit 1 || true
          test -f /usr/share/licenses/sil/mcap/LICENSE
          test -f /usr/share/licenses/sil/nlohmann-json/LICENSE.MIT
          test -f /opt/sil/python/lib/python3.13/site-packages/lz4-4.4.5.dist-info/licenses/LICENSE
          test -f /opt/sil/python/lib/python3.13/site-packages/zstandard-0.25.0.dist-info/licenses/LICENSE
          test ! -e /src
          test ! -e /build
          test ! -e /root/.cache
          test ! -e /workspace/tests
          test ! -e /usr/local/bin/sil-run-instrumented
          for tool in cc gcc c++ g++ cmake make git; do
            ! command -v "$tool"
          done
        """,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize("delayed", [False, True], ids=["nominal", "delayed"])
def test_acc_reference_run_is_bounded_deterministic_and_matches_native(
    tmp_path: Path, delayed: bool
):
    manifest = tmp_path / "acc.json"
    build_args = [str(manifest)]
    if delayed:
        build_args.append("--delayed-sensing")
    built = _container(
        PRODUCTION_IMAGE,
        tmp_path,
        *build_args,
        entrypoint="sil-acc",
    )

    assert built.returncode == 0, built.stderr
    assert built.stdout.strip() == hashlib.sha256(manifest.read_bytes()).hexdigest()
    _assert_bounded_footprint(PRODUCTION_IMAGE, tmp_path, manifest)
    _assert_deterministic_and_native_equivalent(
        PRODUCTION_IMAGE, tmp_path, manifest
    )


def test_default_entrypoint_supports_no_recording(tmp_path: Path):
    manifest = tmp_path / "acc.json"
    built = _container(
        PRODUCTION_IMAGE, tmp_path, str(manifest), entrypoint="sil-acc"
    )
    run = _container(
        PRODUCTION_IMAGE,
        tmp_path,
        str(manifest),
        "--participant-timeout-ms",
        PARTICIPANT_TIMEOUT_MS,
        "--no-recording",
    )

    assert built.returncode == 0, built.stderr
    assert run.returncode == 0, run.stderr
    _assert_hash_output(run, manifest)
    assert not (tmp_path / "out.mcap").exists()


def test_fmi_reference_run_validates_behavior_and_matches_native(tmp_path: Path):
    copied = _container(
        ACCEPTANCE_IMAGE,
        tmp_path,
        "-R",
        "/opt/sil/reference/.",
        str(tmp_path / "reference"),
        entrypoint="cp",
    )
    assert copied.returncode == 0, copied.stderr

    manifest_script = tmp_path / "reference" / "examples" / "fmu" / "manifest.py"
    manifest = tmp_path / "fmu.json"
    built = _container(
        ACCEPTANCE_IMAGE,
        tmp_path,
        str(manifest_script),
        str(manifest),
        entrypoint="python3",
    )
    assert built.returncode == 0, built.stderr

    _assert_bounded_footprint(ACCEPTANCE_IMAGE, tmp_path, manifest)
    _assert_deterministic_and_native_equivalent(
        ACCEPTANCE_IMAGE, tmp_path, manifest
    )

    schemas = load_schemas(
        json.loads((tmp_path / "reference" / "schemas" / "fmu.json").read_text())
    )
    messages: dict[str, list[tuple[int, dict]]] = {"fmu.In": [], "fmu.Out": []}
    for channel, timestamp, payload in read_records(tmp_path / "container-1.mcap"):
        messages[channel].append((timestamp, schemas[channel].unpack(payload)))
    assert [
        (timestamp, values["Float64_continuous_output"],
         values["Float64_discrete_output"])
        for timestamp, values in messages["fmu.Out"][1:]
    ] == [
        (timestamp + 10_000_000, values["Float64_continuous_input"],
         values["Float64_discrete_input"])
        for timestamp, values in messages["fmu.In"][:-1]
    ]


def test_wedged_process_participant_cannot_hang_the_container(tmp_path: Path):
    manifest = tmp_path / "wedged.json"
    manifest.write_text(
        json.dumps(
            {
                "sil_manifest": 1,
                "duration_ns": 1,
                "schemas": {},
                "channels": {},
                "participants": {
                    "wedged": {
                        "type": "process",
                        "command": ["python3", "-c", "import time; time.sleep(3600)"],
                        "step_period_ns": 1,
                        "subscribes": [],
                        "publishes": [],
                    }
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    run = _container(
        PRODUCTION_IMAGE,
        tmp_path,
        str(manifest),
        "--participant-timeout-ms",
        "100",
        "--no-recording",
        timeout=10,
    )

    assert run.returncode == 1
    assert "wedged" in run.stderr
    assert "timeout" in run.stderr
