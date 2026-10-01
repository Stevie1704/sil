"""The ADAS reference library as a process-isolated C-library adapter (#229).

`examples/adas-reference/process_adapter.py` loads the same library build the
Native participant loads, with `ctypes`, and runs it as a Process
participant. These tests hold it to the Native form: every maneuver and every
experiment must give byte-identical Command Messages, and its diagnostics
must name the same causes. Only the execution form differs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, load_module
from test_example_adas_reference import STIMULUS, comparison, comparison_with, library

from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records

EXAMPLE = ROOT / "examples" / "adas-reference"
MS = 1_000_000

prepare = load_module("adas_prepare_229", EXAMPLE / "prepare.py")
manifest = load_module("adas_manifest_229", EXAMPLE / "manifest.py")


@pytest.fixture(scope="module")
def prepared(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("adas-process-prepared")
    prepare.prepare(out)
    return out


def process(build_dir: Path, variant: str = "", **kwargs):
    return manifest.process_controller(library(build_dir, variant),
                                       python=sys.executable, **kwargs)


def run(sil_run: Path, m: Manifest, workdir: Path) -> subprocess.CompletedProcess:
    workdir.mkdir(parents=True, exist_ok=True)
    path = m.write(workdir / "manifest.json").path
    return subprocess.run(
        [str(sil_run), str(path), "-o", str(workdir / "out.mcap"),
         "--participant-timeout-ms", "30000"],
        capture_output=True, text=True, env=dict(os.environ))


def commands(recording: Path) -> list[tuple[str, int, bytes]]:
    return [(channel, t, data) for channel, t, data in read_records(recording)
            if channel.endswith(".command")]


@pytest.fixture(scope="module")
def nominal(build_dir, sil_run, prepared, tmp_path_factory):
    """Every maneuver side by side, once per form."""
    out = tmp_path_factory.mktemp("adas-process-nominal")
    for form, controller in (
            ("native", manifest.native_controller(library(build_dir))),
            ("process", process(build_dir))):
        proc = run(sil_run, manifest.reference_manifest(
            prepared, None, controller=controller), out / form)
        assert proc.returncode == 0, proc.stderr
    return out / "native" / "out.mcap", out / "process" / "out.mcap"


class TestEquivalence:
    @pytest.mark.parametrize("maneuver", manifest.MANEUVERS)
    def test_each_maneuver_matches_its_enumerated_trajectory(
            self, nominal, prepared, maneuver):
        _, process_recording = nominal
        report = comparison(prepared, maneuver, process_recording)
        assert report["verdict"] == "pass", report["first_divergence"]

    def test_the_commands_equal_the_native_commands_byte_for_byte(
            self, nominal):
        native, process_recording = nominal
        assert commands(process_recording) == commands(native)
        assert len(commands(native)) == 20 * len(manifest.MANEUVERS)

    @pytest.mark.parametrize("experiment", sorted(manifest.EXPERIMENTS))
    def test_each_experiment_matches_its_enumerated_trajectory(
            self, build_dir, sil_run, prepared, tmp_path, experiment):
        m = manifest.experiment_manifest(prepared, None, experiment,
                                         controller=process(build_dir))
        proc = run(sil_run, m, tmp_path)
        assert proc.returncode == 0, proc.stderr
        report = comparison_with(prepared, f"cadence.{experiment}",
                                 tmp_path / "out.mcap")
        assert report["verdict"] == "pass", report["first_divergence"]

    def test_only_the_controller_entry_differs_from_the_native_form(
            self, build_dir, prepared):
        native = manifest.reference_manifest(
            prepared, library(build_dir)).to_doc()
        isolated = manifest.reference_manifest(
            prepared, None, controller=process(build_dir)).to_doc()
        for maneuver in manifest.MANEUVERS:
            native["participants"].pop(maneuver)
            isolated["participants"].pop(maneuver)
        assert isolated == native

    def test_a_wrong_sign_library_fails_the_comparison(
            self, build_dir, sil_run, prepared, tmp_path):
        m = manifest.reference_manifest(
            prepared, None, maneuvers=("hazard",),
            controller=process(build_dir, "wrong_sign"))
        assert run(sil_run, m, tmp_path).returncode == 0
        report = comparison(prepared, "hazard", tmp_path / "out.mcap")
        assert report["verdict"] == "fail"
        assert report["first_divergence"]["field"] == "mode"


def stimulated(build_dir: Path, *overrides: str) -> Manifest:
    """One isolated controller fed live by the stimulus participant."""
    m = Manifest(duration_ns=50 * MS)
    m.add_schemas(manifest.SCHEMAS)
    for role, schema in manifest.INPUTS.items():
        m.add_channel(f"live.{role}", schema=schema, latency_ns=0)
    m.add_channel("live.command", schema="adas.Command",
                  latency_ns=manifest.OUTPUT_LATENCY_NS)
    m.add_process("stimulus",
                  command=[sys.executable, str(STIMULUS), "live", *overrides],
                  step_period_ns=manifest.PERIOD_NS,
                  publishes=[f"live.{role}" for role in manifest.INPUTS],
                  priority=-1)
    process(build_dir)(m, "live", [
        SubscriberRoute(f"live.{role}", capacity=manifest.INPUT_ROUTE_CAPACITY)
        for role in manifest.INPUTS])
    return m


class TestFailures:
    @pytest.mark.parametrize("override, diagnostic", [
        ("radar.count=9@2", "t=20000000 ns: radar.count 9 exceeds the capacity 8"),
        ("radar.x_m[5]=1.0@1",
         "t=10000000 ns: radar.x_m[5] is inactive (count 2) but not zero; "
         "inactive elements must be zero"),
        ("ego.speed_mps=-1.0@1", "t=10000000 ns: ego.speed_mps -1 is negative"),
    ])
    def test_an_invalid_input_fails_the_run_with_the_native_diagnostic(
            self, build_dir, sil_run, tmp_path, override, diagnostic):
        proc = run(sil_run, stimulated(build_dir, override), tmp_path)
        assert proc.returncode == 1, proc.stderr
        assert "participant 'live' failed" in proc.stderr
        assert diagnostic in proc.stderr

    @pytest.mark.parametrize("override, diagnostic", [
        ({"period_ns": 20_000_000},
         "period_ns 20000000 is not supported; profile 3 runs only at "
         "10000000 ns"),
        ({"profile_version": 2},
         "config names profile 'sil.adas-reference.radar-camera' version 2; "
         "this library implements 'sil.adas-reference.radar-camera' version 3"),
        ({"unexpected": 1}, "config has unknown key 'unexpected'"),
    ])
    def test_an_invalid_configuration_is_a_manifest_error(
            self, build_dir, sil_run, prepared, tmp_path, override,
            diagnostic):
        config = manifest.controller_config("hazard", **override)
        m = manifest.reference_manifest(
            prepared, None, maneuvers=("hazard",),
            controller=process(build_dir, configs={"hazard": config}))
        proc = run(sil_run, m, tmp_path)
        assert proc.returncode == 2, proc.stderr
        assert diagnostic in proc.stderr

    def test_a_library_that_does_not_load_is_a_manifest_error(
            self, sil_run, prepared, tmp_path):
        missing = tmp_path / "missing.so"
        m = manifest.reference_manifest(
            prepared, None, maneuvers=("hazard",),
            controller=manifest.process_controller(missing,
                                                   python=sys.executable))
        proc = run(sil_run, m, tmp_path / "run")
        assert proc.returncode == 2, proc.stderr
        assert f"cannot load library '{missing}'" in proc.stderr
