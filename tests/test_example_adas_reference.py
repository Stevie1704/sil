"""The ADAS reference application through the Native participant ABI (#222,
#223).

`examples/adas-reference/` is a C application with its own API, a Native
adapter around it, and recorded maneuvers of bounded radar and camera object
lists with hand-enumerated expected trajectories. These tests run it at the
Run boundary: every maneuver must match its expectation exactly under
`contract.json`, the Run must repeat byte-identically, instances must keep
their state apart, a wrong-sign build must fail the comparison, and invalid
configuration, malformed lists and invalid input must fail with a diagnostic
that names the cause.
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
from sil.recording import read_records
from sil.schema import MessageType
from sil.testing import run_simulation

EXAMPLE = ROOT / "examples" / "adas-reference"
STIMULUS = ROOT / "tests" / "participants" / "adas_stimulus.py"
MS = 1_000_000
# The #222 maneuvers: one object per sensor, embedded into lists, with their
# expected trajectories unchanged.
ONE_OBJECT = ("clear", "hazard", "release", "unavailable", "boundaries")

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

    def test_the_one_object_maneuvers_list_at_most_one_object(self):
        for maneuver in ONE_OBJECT:
            assert maneuver in prepare.MANEUVERS
            text = (EXAMPLE / "maneuvers" / f"{maneuver}.csv").read_text()
            assert ";" not in text, maneuver

    def test_the_maneuvers_hold_empty_single_and_full_lists(self, prepared):
        lists = MessageType("adas.ObjectList",
                            manifest.SCHEMAS["adas.ObjectList"])
        counts = {lists.unpack(payload)["count"]
                  for maneuver in prepare.MANEUVERS
                  for channel, _, payload in read_records(
                      prepared / f"{maneuver}.inputs.mcap")
                  if not channel.endswith(".ego")}
        assert {0, 1, prepare.CAPACITY} <= counts
        assert max(counts) == prepare.CAPACITY

    def test_preparation_repeats_byte_identically(self, prepared, tmp_path):
        prepare.prepare(tmp_path)
        for path in sorted(prepared.iterdir()):
            if path.suffix != ".json" or "mapping" in path.name:
                assert (tmp_path / path.name).read_bytes() == \
                    path.read_bytes(), path.name
        receipt = json.loads((tmp_path / "hazard.inputs.receipt.json"
                              ).read_text())
        assert receipt["source"]["file"] == "hazard.inputs.csv"
        assert receipt["mapping"]["file"] == "hazard.inputs.mapping.json"

    def test_preparation_rejects_a_list_over_capacity(self, tmp_path):
        source = tmp_path / "long.csv"
        nine = ";".join(f"{i} {10 + i}.0 0.0 0.0 0.875" for i in range(9))
        source.write_text("time_ms,radar,camera,ego_speed_mps\n"
                          f"0,empty,empty,20.0\n10,{nine},empty,20.0\n")
        with pytest.raises(prepare.PreparationError,
                           match="long.csv row 2: radar lists 9 objects, "
                                 "more than the capacity 8; a list is never "
                                 "truncated"):
            prepare.expand(source, tmp_path / "long.inputs.csv")

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
        # each must publish exactly what it publishes in the five-instance Run,
        # and the two-instance Run repeats byte-identically.
        runs = []
        for attempt in ("first", "second"):
            workdir = tmp_path / attempt
            workdir.mkdir()
            runs.append(run_simulation(
                manifest.reference_manifest(prepared, library(build_dir),
                                            maneuvers=("ordering", "turnover")),
                runner=sil_run, workdir=workdir))
        pair, repeat = runs
        assert pair.mcap_path.read_bytes() == repeat.mcap_path.read_bytes()
        for maneuver in ("ordering", "turnover"):
            channel = f"{maneuver}.command"
            assert pair.messages(channel) == nominal.messages(channel)
        assert pair.messages("ordering.command") != pair.messages(
            "turnover.command")


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
        # In the lists, the selected object closing at 20 m/s is no hazard.
        report = comparison(prepared, "ordering", wrong.mcap_path)
        assert report["verdict"] == "fail"
        assert report["first_divergence"]["field"] == "mode"


class TestConfiguration:
    @pytest.mark.parametrize("override, diagnostic", [
        ({"period_ns": 20_000_000},
         "period_ns 20000000 is not supported; profile 2 runs only at "
         "10000000 ns"),
        ({"hazard_acceleration_mps2": 0.0},
         "hazard_acceleration_mps2 0 is outside [-10, 0)"),
        ({"hazard_acceleration_mps2": -11.0},
         "hazard_acceleration_mps2 -11 is outside [-10, 0)"),
        ({"max_change_mps2": 0.0}, "max_change_mps2 0 is outside (0, 10]"),
        ({"max_change_mps2": 10.5},
         "max_change_mps2 10.5 is outside (0, 10]"),
        ({"profile_version": 1},
         "config names profile 'sil.adas-reference.radar-camera' version 1; "
         "this library implements 'sil.adas-reference.radar-camera' version 2"),
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

    def test_an_invalid_list_makes_the_sensing_unavailable(
        self, build_dir, sil_run, tmp_path
    ):
        result = run_simulation(
            stimulated(library(build_dir), "camera.validity=0@1",
                       "camera.count=0@1", "camera.object_id[0]=0@1",
                       "camera.x_m[0]=0.0@1", "camera.y_m[0]=0.0@1",
                       "camera.confidence[0]=0.0@1"),
            runner=sil_run, workdir=tmp_path)
        modes = [(m["mode"], m["selected_object_id"])
                 for _, m in result.messages("live.command")]
        assert modes == [(0, 7), (2, -1), (0, 7), (0, 7), (0, 7)]

    @pytest.mark.parametrize("override, diagnostic", [
        ("radar.x_m[0]=nan@2",
         "t=20000000 ns: radar.x_m[0] is not finite: nan"),
        ("camera.confidence[0]=inf@1",
         "t=10000000 ns: camera.confidence[0] is not finite: inf"),
        ("radar.relative_vx_mps[1]=-inf@1",
         "t=10000000 ns: radar.relative_vx_mps[1] is not finite: -inf"),
        ("ego.speed_mps=-inf@3",
         "t=30000000 ns: ego.speed_mps is not finite: -inf"),
        ("radar.sample_time_ns=15000000@2",
         "t=20000000 ns: radar.sample_time_ns 15000000 is not the activation "
         "time 20000000; inputs sampled at t are consumed at t"),
        ("camera.sample_time_ns=0@1",
         "t=10000000 ns: camera.sample_time_ns 0 is not the activation time "
         "10000000"),
        ("radar.count=9@0", "t=0 ns: radar.count 9 exceeds the capacity 8"),
        ("camera.count=4294967295@1",
         "t=10000000 ns: camera.count 4294967295 exceeds the capacity 8"),
        ("radar.x_m[5]=1.0@1",
         "t=10000000 ns: radar.x_m[5] is inactive (count 2) but not zero; "
         "inactive elements must be zero"),
        ("camera.object_id[7]=4@0",
         "t=0 ns: camera.object_id[7] is inactive (count 1) but not zero"),
        ("radar.object_id[0]=-1@0",
         "t=0 ns: radar.object_id[0] -1 is negative; active IDs are >= 0 and "
         "-1 is reserved for no selection"),
        ("radar.object_id[1]=7@1",
         "t=10000000 ns: radar.object_id[1] 7 repeats radar.object_id[0]"),
        ("camera.confidence[0]=1.5@1",
         "t=10000000 ns: camera.confidence[0] 1.5 is outside [0, 1]"),
        ("camera.relative_vx_mps[0]=1.0@0",
         "t=0 ns: camera.relative_vx_mps[0] 1 is not 0; this sensor reports "
         "no speed"),
        ("radar.frame_id=2@0",
         "t=0 ns: radar.frame_id 2 is not the ego frame 1; the profile "
         "transforms no frame"),
        ("camera.sensor_id=1@0",
         "t=0 ns: camera.sensor_id 1 is not the camera sensor 2"),
        ("radar.validity=2@0",
         "t=0 ns: radar.validity 2 is neither 0 (invalid) nor 1 (valid)"),
        ("ego.speed_mps=-1.0@1", "t=10000000 ns: ego.speed_mps -1 is negative"),
    ])
    def test_an_invalid_input_fails_the_run_with_its_context(
        self, build_dir, sil_run, tmp_path, override, diagnostic
    ):
        proc = run_at_boundary(sil_run, tmp_path,
                               stimulated(library(build_dir), override))
        assert proc.returncode == 1, proc.stderr
        assert "participant 'live' failed: " + diagnostic in proc.stderr

    def test_a_second_list_in_one_slot_overflows_the_finite_route(
        self, build_dir, sil_run, tmp_path
    ):
        proc = run_at_boundary(sil_run, tmp_path,
                               stimulated(library(build_dir), "radar*2@1"))
        assert proc.returncode == 1, proc.stderr
        assert ("subscriber route capacity exceeded: Channel 'live.radar', "
                "publisher 'stimulus', subscriber 'live', configured "
                "capacity 1") in proc.stderr


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
