"""The ADAS reference application through the Native participant ABI (#222).

`examples/adas-reference/` is a C application with its own API, a Native
adapter around it, and five recorded maneuvers with hand-enumerated expected
trajectories. These tests run it at the Run boundary: every maneuver must
match its expectation exactly under `contract.json`, the Run must repeat
byte-identically, instances must keep their state apart, a wrong-sign build
must fail the comparison, and invalid configuration and input must fail with
a diagnostic that names the cause.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, load_module
from test_staged_install import installed_environment

from sil.compare import compare, read_contract
from sil.manifest import SubscriberRoute
from sil.testing import run_simulation

EXAMPLE = ROOT / "examples" / "adas-reference"
STIMULUS = ROOT / "tests" / "participants" / "adas_stimulus.py"
MS = 1_000_000

prepare = load_module("adas_prepare", EXAMPLE / "prepare.py")
manifest = load_module("adas_manifest", EXAMPLE / "manifest.py")


def library(build_dir: Path, variant: str = "") -> Path:
    path = build_dir / f"adas_reference{'_' + variant if variant else ''}.so"
    assert path.is_file(), f"library build missing at {path}"
    return path


@pytest.fixture(scope="module")
def prepared(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("adas-prepared")
    prepare.prepare(out)
    return out


@pytest.fixture(scope="module")
def nominal(build_dir, sil_run, prepared, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("adas-run")
    return run_simulation(
        manifest.reference_manifest(prepared, library(build_dir)),
        runner=sil_run, workdir=workdir,
    )


def comparison(prepared: Path, maneuver: str, recording: Path) -> dict:
    contract = read_contract(prepared / f"{maneuver}.contract.json")
    return compare(contract, recording,
                   prepared / f"{maneuver}.expected.mcap")


def run_at_boundary(sil_run, tmp_path: Path, m) -> subprocess.CompletedProcess:
    ref = m.write(tmp_path / "manifest.json")
    return subprocess.run(
        [str(sil_run), str(ref.path), "-o", str(tmp_path / "run.mcap")],
        cwd=tmp_path, capture_output=True, text=True)


class TestProfile:
    def test_the_mappings_declare_the_schemas_the_library_is_built_from(self):
        schemas = json.loads((EXAMPLE / "schemas.json").read_text())
        for name in ("mapping.json", "expected-mapping.json"):
            mapping = json.loads((EXAMPLE / name).read_text())
            for schema, declared in mapping["schemas"].items():
                assert declared == schemas[schema], (name, schema)

    def test_the_application_includes_no_sil_header(self):
        for name in ("adas_reference.h", "adas_reference.c"):
            text = (EXAMPLE / name).read_text()
            assert "sil/" not in text and "adas_messages.h" not in text

    def test_every_maneuver_is_enumerated_for_every_activation(self):
        # Twenty activations at 0..190 ms; one expected row per Sample time.
        for maneuver in prepare.MANEUVERS:
            rows = (EXAMPLE / "maneuvers" / f"{maneuver}.expected.csv"
                    ).read_text().splitlines()[1:]
            assert [int(r.split(",")[0]) for r in rows] == list(
                range(10, 201, 10)), maneuver


class TestNominal:
    @pytest.mark.parametrize("maneuver", prepare.MANEUVERS)
    def test_each_maneuver_matches_its_enumerated_trajectory(
        self, nominal, prepared, maneuver
    ):
        report = comparison(prepared, maneuver, nominal.mcap_path)
        assert report["verdict"] == "pass", report["first_divergence"]
        (channel,) = report["channels"].values()
        assert channel["checked"] == 20

    def test_the_command_is_published_at_t_for_sample_time_t_plus_period(
        self, nominal
    ):
        hazard = nominal.messages("hazard.command")
        assert [t for t, _ in hazard] == list(range(0, 200 * MS, 10 * MS))
        assert all(m["sample_time_ns"] == t + 10 * MS for t, m in hazard)
        # Rate-limited from 0 toward -3 m/s^2 by 0.5 m/s^2 per activation.
        assert [m["acceleration_mps2"] for _, m in hazard[:7]] == [
            -0.5, -1.0, -1.5, -2.0, -2.5, -3.0, -3.0]

    def test_signed_selected_ids_keep_zero_and_int32_max(self, nominal):
        selected = [m["selected_object_id"]
                    for _, m in nominal.messages("boundaries.command")]
        assert selected[:2] == [0, 2**31 - 1]
        assert -1 in selected

    def test_the_run_repeats_byte_identically(
        self, build_dir, sil_run, prepared, tmp_path
    ):
        runs = []
        for attempt in ("first", "second"):
            workdir = tmp_path / attempt
            workdir.mkdir()
            runs.append(run_simulation(
                manifest.reference_manifest(prepared, library(build_dir)),
                runner=sil_run, workdir=workdir))
        first, second = runs
        assert first.manifest_hash == second.manifest_hash
        assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()


class TestInstances:
    def test_an_instance_alone_publishes_what_it_publishes_beside_others(
        self, build_dir, sil_run, prepared, nominal, tmp_path
    ):
        # Two instances of the one loaded library with different inputs:
        # each must publish exactly what it publishes in the five-instance Run.
        pair = run_simulation(
            manifest.reference_manifest(prepared, library(build_dir),
                                        maneuvers=("hazard", "clear")),
            runner=sil_run, workdir=tmp_path)
        for maneuver in ("hazard", "clear"):
            channel = f"{maneuver}.command"
            assert pair.messages(channel) == nominal.messages(channel)
        assert pair.messages("hazard.command") != pair.messages(
            "clear.command")


class TestWrongSign:
    def test_a_sign_error_fails_the_comparison(
        self, build_dir, sil_run, prepared, tmp_path
    ):
        wrong = run_simulation(
            manifest.reference_manifest(
                prepared, library(build_dir, "wrong_sign")),
            runner=sil_run, workdir=tmp_path)
        report = comparison(prepared, "hazard", wrong.mcap_path)
        assert report["verdict"] == "fail"
        first = report["first_divergence"]
        assert (first["channel"], first["field"]) == ("hazard.command", "mode")
        assert (first["actual"], first["expected"]) == (0, 1)


class TestConfiguration:
    @pytest.mark.parametrize("override, diagnostic", [
        ({"period_ns": 20_000_000},
         "period_ns 20000000 is not supported; profile 1 runs only at "
         "10000000 ns"),
        ({"hazard_acceleration_mps2": 0.0},
         "hazard_acceleration_mps2 0 is outside [-10, 0)"),
        ({"hazard_acceleration_mps2": -11.0},
         "hazard_acceleration_mps2 -11 is outside [-10, 0)"),
        ({"max_change_mps2": 0.0}, "max_change_mps2 0 is outside (0, 10]"),
        ({"max_change_mps2": 10.5},
         "max_change_mps2 10.5 is outside (0, 10]"),
        ({"profile_version": 2},
         "config names profile 'sil.adas-reference.radar-camera' version 2; "
         "this library implements 'sil.adas-reference.radar-camera' version 1"),
        ({"period_ns": 1e7},
         "config key 'period_ns' is not an unsigned 64-bit integer"),
        ({"unexpected": 1}, "config has unknown key 'unexpected'"),
        ({"radar": 5}, "config key 'radar' must be a string"),
    ])
    def test_invalid_configuration_fails_before_stepping(
        self, build_dir, sil_run, prepared, tmp_path, override, diagnostic
    ):
        config = manifest.controller_config("hazard", **override)
        proc = run_at_boundary(sil_run, tmp_path, manifest.reference_manifest(
            prepared, library(build_dir), maneuvers=("hazard",),
            configs={"hazard": config}))
        assert proc.returncode == 2, proc.stderr
        assert "participant 'hazard'" in proc.stderr
        assert diagnostic in proc.stderr

    def test_a_missing_key_is_named(
        self, build_dir, sil_run, prepared, tmp_path
    ):
        config = manifest.controller_config("hazard")
        del config["max_change_mps2"]
        proc = run_at_boundary(sil_run, tmp_path, manifest.reference_manifest(
            prepared, library(build_dir), maneuvers=("hazard",),
            configs={"hazard": config}))
        assert proc.returncode == 2, proc.stderr
        assert "config is missing key 'max_change_mps2'" in proc.stderr


def stimulated(library_path: Path, *overrides: str):
    """One controller fed live by the stimulus participant, which runs
    before it in each Slot."""
    m = manifest.Manifest(duration_ns=50 * MS)
    m.add_schemas(manifest.SCHEMAS)
    for role, schema in manifest.INPUTS.items():
        m.add_channel(f"live.{role}", schema=schema, latency_ns=0)
    m.add_channel("live.command", schema="adas.Command",
                  latency_ns=manifest.OUTPUT_LATENCY_NS)
    m.add_process(
        "stimulus",
        command=[sys.executable, str(STIMULUS), "live", *overrides],
        step_period_ns=manifest.PERIOD_NS,
        publishes=[f"live.{role}" for role in manifest.INPUTS],
        priority=-1,
    )
    m.add_native(
        "live", library=str(library_path),
        config=manifest.controller_config("live"),
        subscribes=[SubscriberRoute(f"live.{role}", capacity=1)
                    for role in manifest.INPUTS],
        publishes=["live.command"],
    )
    return m


class TestInputs:
    def test_live_stimulus_reaches_the_controller(
        self, build_dir, sil_run, tmp_path
    ):
        result = run_simulation(stimulated(library(build_dir)),
                                runner=sil_run, workdir=tmp_path)
        commands = result.messages("live.command")
        assert len(commands) == 5
        assert all(m["mode"] == 0 and m["selected_object_id"] == 7
                   for _, m in commands)

    @pytest.mark.parametrize("override, diagnostic", [
        ("radar.x_m=nan@2", "t=20000000 ns: radar.x_m is not finite: nan"),
        ("camera.confidence=inf@1",
         "t=10000000 ns: camera.confidence is not finite: inf"),
        ("ego.speed_mps=-inf@3",
         "t=30000000 ns: ego.speed_mps is not finite: -inf"),
        ("radar.sample_time_ns=15000000@2",
         "t=20000000 ns: radar.sample_time_ns 15000000 is not the activation "
         "time 20000000; inputs sampled at t are consumed at t"),
        ("camera.sample_time_ns=0@1",
         "t=10000000 ns: camera.sample_time_ns 0 is not the activation time "
         "10000000"),
        ("radar.object_id=-2@0",
         "t=0 ns: radar.object_id -2 is neither -1 (no object) nor >= 0"),
        ("ego.speed_mps=-1.0@1", "t=10000000 ns: ego.speed_mps -1 is negative"),
    ])
    def test_an_invalid_input_fails_the_run_with_its_context(
        self, build_dir, sil_run, tmp_path, override, diagnostic
    ):
        proc = run_at_boundary(sil_run, tmp_path,
                               stimulated(library(build_dir), override))
        assert proc.returncode == 1, proc.stderr
        assert "participant 'live' failed: " + diagnostic in proc.stderr


@pytest.mark.skipif(shutil.which("cc") is None, reason="needs a C compiler")
def test_the_demonstration_runs_on_installed_interfaces(
    staged_prefix: Path, installed_python: Path, tmp_path: Path
):
    """run.sh builds against the staged include/sil and silschema only, and
    drives the installed sil-run, sil-csv conversion and sil-compare."""
    env = installed_environment(installed_python)
    env["PATH"] = str(staged_prefix / "bin") + os.pathsep + env["PATH"]
    proc = subprocess.run(
        [str(EXAMPLE / "run.sh"), str(tmp_path / "demo")],
        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for maneuver in prepare.MANEUVERS:
        assert f"{maneuver}: pass" in proc.stdout
    assert "wrong sign: fails as required" in proc.stdout
