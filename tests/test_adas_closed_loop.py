"""A processed-sensor loop with interchangeable native and FMU controllers
(issue #227).

`proofs/adas-closed-loop/` runs every case on Linux x86-64 with the
qualified ACC plant FMU and an independent FMPy execution. These tests run
what needs neither: the edge conversions, the consumption table, the
predictions and the KPIs, and every case through installed-equivalent SiL
with a stand-in for the plant FMU that steps the same dynamics under the
importer's time convention. With a host build of `AdasReference.fmu`, one
case also runs through the FMU form.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ROOT, run_manifest

from sil.compare import compare, read_contract
from sil.participant import Input, ManifestError, ParticipantFailure
from sil.recording import read_records
from sil.schema import MessageType

PROOF_DIR = ROOT / "proofs" / "adas-closed-loop"
STANDIN = ROOT / "tests" / "participants" / "standin_acc_plant.py"
sys.path.insert(0, str(PROOF_DIR))
try:
    import edge
    import independent
    import kpi
    import loop
finally:
    sys.path.remove(str(PROOF_DIR))

MS = loop.MS
H = loop.STEP_NS
C, HZ, U = loop.CLEAR, loop.HAZARD, loop.UNAVAILABLE
COMMAND = MessageType("adas.Command", loop.SCHEMAS["adas.Command"])
TRUTH = MessageType("loop.Truth", loop.SCHEMAS["loop.Truth"])


def standin_plant(case: loop.Case) -> list[str]:
    return ["python3", str(STANDIN), repr(case.initial_gap_m)]


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory) -> tuple[Path, Path]:
    """Stand-ins for the library and the archive: Manifests name them."""
    out = tmp_path_factory.mktemp("adas-closed-loop-artifacts")
    (out / "adas_reference.so").write_bytes(b"library")
    (out / "AdasReference.fmu").write_bytes(b"archive")
    return out / "adas_reference.so", out / "AdasReference.fmu"


def docs(case, artifacts) -> tuple[dict, dict]:
    library, archive = artifacts
    return (loop.loop_manifest(case, loop.native_controller(library),
                               standin_plant),
            loop.loop_manifest(case, loop.fmu_controller(archive),
                               standin_plant))


# --- edge conversions ----------------------------------------------------------


class TestConversion:
    def test_a_plant_value_rounds_once_to_the_nearest_binary32(self):
        assert edge.to_f32("x", 0.1) == 0.10000000149011612
        assert edge.to_f32("x", 30.0) == 30.0

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), 1e39])
    def test_a_value_binary32_cannot_carry_is_refused(self, value):
        with pytest.raises(edge.ConversionError, match="x_m"):
            edge.to_f32("radar.x_m", value)

    def test_the_two_paths_round_alike(self):
        for value in (0.1, 29.987654321, -6.000000001, 1e-30):
            assert edge.to_f32("x", value) == independent.binary32(value)

    def test_a_negative_ego_speed_is_refused(self):
        truth = loop.initial_truth(30.0) | {"ego_speed_mps": -0.25}
        with pytest.raises(edge.ConversionError, match="negative"):
            edge.ego_motion(0, 0, truth)

    def test_a_visible_lead_is_one_object_at_the_gap(self):
        truth = loop.initial_truth(30.0) | {"gap_m": 29.9,
                                            "relative_speed_mps": -0.1}
        radar = edge.object_list("radar", 20 * MS, 2, truth, True,
                                 loop.SENSOR_OBJECTS["radar"])
        assert radar["count"] == 1 and radar["sensor_id"] == 1
        assert radar["x_m"][:2] == [edge.to_f32("x", 29.9), 0]
        assert radar["relative_vx_mps"][0] == edge.to_f32("v", -0.1)
        camera = edge.object_list("camera", 0, 0, truth, True,
                                  loop.SENSOR_OBJECTS["camera"])
        assert camera["relative_vx_mps"][0] == 0.0
        assert camera["object_id"][0] == 7

    def test_a_hidden_lead_is_an_empty_valid_list(self):
        hidden = edge.object_list("radar", 0, 0, loop.initial_truth(30.0),
                                  False, loop.SENSOR_OBJECTS["radar"])
        assert hidden["count"] == 0 and hidden["validity"] == 1
        assert all(v == 0 for name in ("object_id", "x_m", "confidence")
                   for v in hidden[name])

    def test_both_paths_derive_the_same_observation(self):
        declaration = loop.independent_declaration(loop.CASES["cut_out"])
        truth = loop.initial_truth(30.0) | {"gap_m": 12.345,
                                            "relative_speed_mps": -3.21}
        for sensor in ("radar", "camera"):
            ours = edge.object_list(sensor, 40 * MS, 3, truth, True,
                                    loop.SENSOR_OBJECTS[sensor])
            theirs = independent.observation(declaration, sensor, 40 * MS, 3,
                                             truth)
            assert {k: v[0] if len(v) == 1 else v
                    for k, v in theirs.items()} == ours


class TestEdgeParticipants:
    def sensor(self, **change) -> edge.Sensor:
        config = loop._sensor_config(loop.CASES["approach"], "radar")
        return edge.Sensor(config | change)

    def visibility(self, t: int) -> Input:
        return Input("loop.visibility", t,
                     {"sample_time_ns": t, "lead_visible": 1})

    def truth(self, published: int) -> Input:
        return Input("loop.truth", published, loop.initial_truth(30.0))

    def test_the_first_list_describes_the_initial_condition(self):
        [(channel, fields)] = self.sensor().on_step(0, 20 * MS,
                                                    [self.visibility(0)])
        assert channel == "adas.radar"
        assert fields["sample_time_ns"] == 0 and fields["x_m"][0] == 30.0

    def test_the_sensor_takes_the_newest_truth_sampled_at_t(self):
        sensor = self.sensor()
        sensor.on_step(0, 20 * MS, [self.visibility(0)])
        [(_, fields)] = sensor.on_step(20 * MS, 20 * MS, [
            self.truth(0), self.visibility(10 * MS), self.truth(10 * MS),
            self.visibility(20 * MS)])
        assert fields["sample_time_ns"] == 20 * MS and fields["sequence"] == 1

    def test_truth_sampled_after_the_activation_is_refused(self):
        with pytest.raises(ParticipantFailure,
                           match="sampled at 10000000 ns, after"):
            self.sensor().on_step(0, 20 * MS, [self.truth(0),
                                               self.visibility(0)])

    def test_missing_truth_is_refused(self):
        sensor = self.sensor()
        sensor.on_step(0, 20 * MS, [self.visibility(0)])
        with pytest.raises(ParticipantFailure, match="sampled at 0 ns, before"):
            sensor.on_step(20 * MS, 20 * MS, [self.visibility(20 * MS)])

    def actuator(self) -> edge.Actuator:
        return edge.Actuator({"command": "adas.command",
                              "actuation": "loop.actuation",
                              "initial_accel_mps2": 0.0})

    def command(self, sample: int, accel: float) -> Input:
        fields = {f: 0 for f in loop.COMMAND_FIELDS}
        return Input("adas.command", sample - H,
                     fields | {"sample_time_ns": sample,
                               "acceleration_mps2": accel})

    def test_the_actuator_applies_the_initial_then_the_due_command(self):
        actuator = self.actuator()
        assert actuator.on_step(0, H, []) == [
            ("loop.actuation", {"accel_mps2": 0.0})]
        accel = edge.to_f32("a", -0.1)
        assert actuator.on_step(H, H, [self.command(H, accel)]) == [
            ("loop.actuation", {"accel_mps2": accel})]

    @pytest.mark.parametrize("commands", [[], [2 * H], [H, H]])
    def test_the_actuator_refuses_any_other_command(self, commands):
        actuator = self.actuator()
        actuator.on_step(0, H, [])
        with pytest.raises(ParticipantFailure, match="exactly the one"):
            actuator.on_step(H, H, [self.command(s, 0.0) for s in commands])

    def test_a_bad_config_is_a_manifest_error(self):
        participant = edge._Deferred("actuator", json.dumps(
            {"command": "c", "actuation": "a",
             "initial_accel_mps2": "nan"}))
        with pytest.raises(ManifestError, match="finite number"):
            participant.on_init({})
        with pytest.raises(ManifestError, match="config keys"):
            edge._Deferred("sensor", "{}").on_init({})


# --- the consumption table -------------------------------------------------------


class TestTable:
    @pytest.mark.parametrize("name", sorted(loop.ALL_CASES))
    def test_only_the_controller_differs_between_the_forms(self, name,
                                                           artifacts):
        native, fmu = docs(loop.ALL_CASES[name], artifacts)
        differences = loop.form_difference(native, fmu)
        assert differences and all(d.startswith("participants.controller.")
                                   for d in differences)

    def test_every_consumption_is_stated(self, artifacts):
        native, _ = docs(loop.CASES["approach"], artifacts)
        table = {(r["participant"], r["channel"]): r
                 for r in loop.consumption_table(native)}
        assert set(table) == {
            ("radar", "loop.truth"), ("radar", "loop.visibility"),
            ("camera", "loop.truth"), ("camera", "loop.visibility"),
            ("ego", "loop.truth"), ("controller", "adas.radar"),
            ("controller", "adas.camera"), ("controller", "adas.ego"),
            ("actuator", "adas.command"), ("plant", "loop.lead"),
            ("plant", "loop.actuation")}
        truth = table[("camera", "loop.truth")]
        assert (truth["period_ns"], truth["latency_ns"],
                truth["sample_offset_ns"], truth["capacity"]) == \
            (40 * MS, H, H, 4)
        command = table[("actuator", "adas.command")]
        assert command["least_delay_ns"] == command["sample_offset_ns"] == H
        assert table[("plant", "loop.actuation")]["within_slot"] == \
            "publisher first"

    def test_the_declared_periods_and_latencies(self, artifacts):
        native, _ = docs(loop.CASES["approach"], artifacts)
        periods = {name: p.get("step_period_ns", p.get("config", {})
                               .get("period_ns"))
                   for name, p in native["participants"].items()}
        assert periods == {"maneuver": H, "radar": 20 * MS,
                           "camera": 40 * MS, "ego": H, "controller": H,
                           "actuator": H, "plant": H}
        assert {name: c["latency_ns"] for name, c in
                native["channels"].items()} == {
            "loop.lead": 0, "loop.visibility": 0, "loop.truth": H,
            "adas.radar": 0, "adas.camera": 0, "adas.ego": 0,
            "adas.command": H, "loop.actuation": 0}

    def test_a_future_observation_is_refused(self, artifacts):
        native, _ = docs(loop.CASES["approach"], artifacts)
        loop._future_truth(native)
        findings = loop.consumption_findings(native)
        assert [f for f in findings if "future observation" in f] == [
            f"{s} <- loop.truth: takes a value sampled 10000000 ns after its "
            "activation (a future observation)"
            for s in ("radar", "camera", "ego")]

    def test_an_unstated_within_slot_order_is_refused(self, artifacts):
        native, _ = docs(loop.CASES["approach"], artifacts)
        native["participants"]["actuator"]["priority"] = loop.PRIORITY["plant"]
        assert loop.consumption_findings(native) == [
            "plant <- loop.actuation: Latency 0 at equal priority states "
            "no within-Slot order"]

    def test_the_plant_binds_every_field_and_starts_the_lead(self):
        """The importer takes declared bindings as the whole mapping and
        refuses an unbound Channel field, so every field is bound and the
        initial lead position is a start value, not a Channel field."""
        command = loop.fmu_plant(Path("AccPlant.fmu"))(loop.CASES["near_start"])
        binds = [command[i + 1] for i, a in enumerate(command) if a == "--bind"]
        assert binds == [
            "loop.lead:lead_accel_mps2=lead_accel_mps2",
            "loop.actuation:accel_mps2=accel_mps2",
            *(f"loop.truth:{n}={n}" for n in loop.TRUTH_FIELDS)]
        assert command[-2:] == ["--start", "initial_lead_position_m=7.0"]

    def test_the_authoring_check_refuses_a_bad_period(self):
        case = replace(loop.CASES["approach"],
                       sensor_periods_ns={"radar": 30 * MS, "camera": 40 * MS,
                                          "ego": H})
        with pytest.raises(loop.TableError, match="radar: Period 30000000"):
            loop.loop_manifest(case, loop.native_controller(Path("x.so")),
                               standin_plant)

    @pytest.mark.parametrize("variant", sorted(loop.EFFECTS))
    def test_a_variant_changes_only_what_it_isolates(self, variant,
                                                     artifacts):
        baseline = loop.EFFECTS[variant].compared_with
        found = loop.effect_difference(
            variant, docs(loop.ALL_CASES[baseline], artifacts)[0],
            docs(loop.ALL_CASES[variant], artifacts)[0])
        assert found

    def test_a_variant_with_another_change_is_refused(self, artifacts):
        baseline = docs(loop.CASES["approach"], artifacts)[0]
        variant = docs(loop.VARIANTS["approach.latency"], artifacts)[0]
        variant["channels"]["loop.truth"]["latency_ns"] = 2 * H
        with pytest.raises(loop.TableError, match="loop.truth"):
            loop.effect_difference("approach.latency", baseline, variant)


# --- predictions and KPIs ------------------------------------------------------------


class TestPredictions:
    def test_the_radar_loss_transitions_follow_the_freshness_limit(self):
        """The list sampled before the drop window is fresh at an age of
        40 ms and stale one Step later; the first list after the window
        recovers. Each Command describes its activation plus one Step."""
        case = loop.CASES["sensor_loss"]
        [drop] = case.interceptors["radar"]
        radar = case.sensor_periods_ns["radar"]
        last_held = drop["start_ns"] - radar
        stale_at = last_held + 40 * MS + H
        recovered_at = -(-drop["end_ns"] // radar) * radar
        assert case.modes == ((C, H), (U, stale_at + H), (C, recovered_at + H))
        assert loop.DELIBERATE["kpi"]["prediction"]["sample_time_ns"] == \
            stale_at + H
        divergence = loop.DELIBERATE["comparison"]["prediction"]
        assert divergence.observation_ns == drop["start_ns"] + H
        assert divergence.actual == drop["start_ns"] - last_held

    def test_the_cut_out_is_seen_at_the_next_radar_list(self):
        case = loop.CASES["cut_out"]
        [(start, _)] = case.hidden
        assert start % case.sensor_periods_ns["radar"] == 0
        assert case.modes[-1] == (C, start + H)

    def test_the_failures_name_the_case_they_change(self):
        assert set(loop.FAILURES) == {"route_overflow", "malformed_list",
                                      "future_truth", "manifest_error"}
        assert all(f.case in loop.CASES for f in loop.FAILURES.values())


class TestKpi:
    def commands(self, modes_accels) -> list:
        return [(k * H, {"sample_time_ns": (k + 1) * H, "sequence": k + 1,
                         "mode": mode, "acceleration_mps2": accel})
                for k, (mode, accel) in enumerate(modes_accels)]

    def truth(self, gaps) -> list:
        return [(k * H, {"gap_m": gap, "ego_speed_mps": 25.0})
                for k, gap in enumerate(gaps)]

    def test_modes_and_their_predicted_starts(self):
        runs = kpi.mode_runs(self.commands([(C, 0), (U, -0.5), (C, 0)]), H)
        assert runs == [(C, H), (U, 2 * H), (C, 3 * H)]
        assert kpi.mode_findings(runs, ((C, H), (U, None), (C, 3 * H))) == []
        assert kpi.mode_findings(runs, ((C, H), (U, 3 * H), (C, None))) == [
            f"mode {U} starts at {2 * H} ns, predicted {3 * H} ns"]
        assert kpi.mode_findings(runs, ((C, H),))

    def test_the_first_violation_is_the_earliest(self):
        commands = self.commands([(C, 0), (U, -0.5)])
        truth = self.truth([30.0, 1.0])
        assert kpi.first_violation(commands, truth, loop.NO_BRAKING_KPI,
                                   H) == {"sample_time_ns": 2 * H,
                                          "field": "gap_m", "value": 1.0,
                                          "bounds": [2.0, float("inf")]}
        assert kpi.first_violation(commands, self.truth([30, 30]), loop.KPI,
                                   H) is None

    def test_coverage_needs_every_slot_through_the_final_one(self):
        commands = self.commands([(C, 0)] * 3)
        assert kpi.coverage(commands, self.truth([30] * 3), H, 3 * H) == []
        assert kpi.coverage(commands[:-1], self.truth([30] * 3), H, 3 * H)

    def test_an_effect_outside_the_envelope_is_found(self):
        base = (self.commands([(C, 0), (HZ, -0.5)]), self.truth([30, 29]))
        late = (self.commands([(C, 0), (C, 0), (C, 0), (HZ, -0.5)]),
                self.truth([30, 29, 28, 27]))
        result = kpi.effect(base, late, HZ, H, loop.EFFECT_ENVELOPE)
        assert result["hazard_onset_ns"] == [2 * H, 4 * H]
        assert any("minimum gap" in f for f in result["findings"])


class TestIndependentSchedule:
    def test_a_dropped_list_uses_its_sequence(self):
        case = loop.independent_declaration(loop.CASES["sensor_loss"])
        sequences = dict.fromkeys(independent.SENSORS, 0)
        truth = loop.initial_truth(60.0)
        assert [s for _, s, _ in independent.published(
            case, 980 * MS, sequences, truth)] == ["radar", "ego"]
        assert [s for _, s, _ in independent.published(
            case, 1_000 * MS, sequences, truth)] == ["camera", "ego"]
        assert sequences == {"radar": 2, "camera": 1, "ego": 2}

    def test_latency_makes_an_observation_due_later(self):
        case = loop.independent_declaration(loop.VARIANTS["approach.latency"])
        out = independent.published(case, 0, dict.fromkeys(
            independent.SENSORS, 0), loop.initial_truth(30.0))
        assert {due for due, _, _ in out} == {H}

    def test_only_drop_faults_are_declared_to_it(self):
        with pytest.raises(loop.TableError, match="drop only"):
            loop.independent_declaration(replace(
                loop.CASES["approach"],
                interceptors=loop.FAILURES["route_overflow"].interceptors))
        assert loop.independent_declaration(loop.CASES["sensor_loss"],
                                            faults=False)["drops"] == {}


# --- Runs through SiL with a stand-in plant ------------------------------------------


needs_build = pytest.mark.skipif(shutil.which("cc") is None,
                                 reason="needs a C compiler")


@pytest.fixture(scope="module")
def env() -> dict[str, str]:
    """The participants' `python3` is this interpreter, with `sil`."""
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
    env["PYTHONPATH"] = str(ROOT / "python" / "src")
    return env


def decode(recording: Path) -> tuple[list, list]:
    commands, truth = [], []
    for channel, t, payload in read_records(recording):
        if channel == "adas.command":
            commands.append((t, COMMAND.unpack(payload)))
        elif channel == "loop.truth":
            truth.append((t, TRUTH.unpack(payload)))
    return commands, truth


def run(sil_run, doc: dict, path: Path, env):
    loop.write(doc, path)
    return run_manifest(sil_run, path, path.with_suffix(".mcap"), env)


@pytest.fixture(scope="module")
def library(build_dir) -> Path:
    return build_dir / "adas_reference.so"


@needs_build
class TestLoopRuns:
    @pytest.mark.parametrize("name", sorted(loop.ALL_CASES))
    def test_each_case_shows_its_modes_within_the_kpis(
            self, name, sil_run, library, env, tmp_path):
        case = loop.ALL_CASES[name]
        doc = loop.loop_manifest(case, loop.native_controller(library),
                                 standin_plant)
        first = run(sil_run, doc, tmp_path / "a.json", env)
        second = run(sil_run, doc, tmp_path / "b.json", env)
        assert first.returncode == 0, first.stderr
        assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()
        commands, truth = decode(first.mcap_path)
        assert kpi.coverage(commands, truth, H, loop.DURATION_NS) == []
        assert kpi.mode_findings(kpi.mode_runs(commands, H), case.modes) == []
        assert kpi.first_violation(commands, truth, loop.KPI, H) is None

    def test_the_variants_stay_in_the_envelope(self, sil_run, library, env,
                                               tmp_path):
        decoded = {}
        for name in ("approach.ideal", "approach", "approach.latency"):
            doc = loop.loop_manifest(loop.ALL_CASES[name],
                                     loop.native_controller(library),
                                     standin_plant)
            proc = run(sil_run, doc, tmp_path / f"{name}.json", env)
            assert proc.returncode == 0, proc.stderr
            decoded[name] = decode(proc.mcap_path)
        for variant, effect in loop.EFFECTS.items():
            result = kpi.effect(decoded[effect.compared_with], decoded[variant],
                                loop.HAZARD, H, loop.EFFECT_ENVELOPE)
            assert result["findings"] == [], (variant, result)

    def test_the_radar_loss_fails_the_no_braking_kpi_where_predicted(
            self, sil_run, library, env, tmp_path):
        spec = loop.DELIBERATE["kpi"]
        doc = loop.loop_manifest(loop.CASES[spec["case"]],
                                 loop.native_controller(library),
                                 standin_plant)
        proc = run(sil_run, doc, tmp_path / "loss.json", env)
        violation = kpi.first_violation(*decode(proc.mcap_path), spec["kpi"],
                                        H)
        assert {k: violation[k] for k in spec["prediction"]} == \
            spec["prediction"]

    @pytest.mark.parametrize("name", sorted(loop.FAILURES))
    def test_a_failure_exits_with_its_code_and_names_its_cause(
            self, name, sil_run, library, env, tmp_path):
        failure = loop.FAILURES[name]
        doc = loop.failure_document(failure, loop.native_controller(library),
                                    standin_plant)
        proc = run(sil_run, doc, tmp_path / "failure.json", env)
        assert proc.returncode == failure.exit_code, proc.stderr
        for diagnostic in failure.diagnostics:
            assert diagnostic in proc.stderr


@pytest.fixture(scope="module")
def fmu(tmp_path_factory) -> Path:
    """This host's build of the controller archive."""
    package = loop.load("adas_closed_loop_test_package",
                        loop.EXAMPLE_DIR / "fmu" / "package.py")
    out = tmp_path_factory.mktemp("adas-closed-loop-fmu")
    package.build(out)
    return out / "AdasReference.fmu"


@needs_build
class TestBothForms:
    def test_the_forms_close_the_loop_alike(self, sil_run, library, fmu,
                                            env, tmp_path):
        case = loop.CASES["sensor_loss"]
        recordings = {}
        for form, controller in (
                ("native", loop.native_controller(library)),
                ("fmu", loop.fmu_controller(fmu))):
            doc = loop.loop_manifest(case, controller, standin_plant)
            proc = run(sil_run, doc, tmp_path / f"{form}.json", env)
            assert proc.returncode == 0, proc.stderr
            recordings[form] = proc.mcap_path
        contract = tmp_path / "cross.json"
        contract.write_text(json.dumps(loop.CROSS_FORM_CONTRACT))
        report = compare(read_contract(contract), recordings["native"],
                         recordings["fmu"])
        assert report["verdict"] == "pass", report["first_divergence"]
        assert report["channels"]

    def test_a_malformed_list_fails_the_fmu_form_too(self, sil_run, fmu, env,
                                                     tmp_path):
        failure = loop.FAILURES["malformed_list"]
        doc = loop.failure_document(failure, loop.fmu_controller(fmu),
                                    standin_plant)
        proc = run(sil_run, doc, tmp_path / "failure.json", env)
        assert proc.returncode == 1
        assert failure.diagnostics[0] in proc.stderr
