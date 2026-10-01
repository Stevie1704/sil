"""Native and FMU ADAS controller forms of one experiment (issue #226).

`proofs/adas-equivalence/` runs every case on Linux x86-64 in a pinned image,
with FMPy as the independent FMI execution. These tests run what needs no
FMPy and no pinned compiler: the two forms of every case differ only in the
controller's target and adaptation, each negative control changes exactly
one thing, each predicted divergence agrees with the hand-enumerated oracle,
and, with a host build of the archive, one case runs through both forms and
one control diverges where it is predicted to.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from conftest import ROOT, load_module, run_manifest

from sil.compare import compare, read_contract

PROOF_DIR = ROOT / "proofs" / "adas-equivalence"


def _registered(name: str, path: Path):
    """A module its own dataclasses can find in sys.modules."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


experiment = _registered("adas_equivalence_experiment",
                         PROOF_DIR / "experiment.py")
prepare = load_module("adas_prepare_226",
                      experiment.EXAMPLE_DIR / "prepare.py")
package = load_module("adas_fmu_package_226",
                      experiment.EXAMPLE_DIR / "fmu" / "package.py")

MANEUVERS = experiment.EXAMPLE_DIR / "maneuvers"


@pytest.fixture(scope="module")
def inputs(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("adas-equivalence-inputs")
    prepare.prepare(out)
    return out


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory) -> tuple[Path, Path]:
    """Stand-ins for the library and the archive: the Manifests name them
    and the form record digests them, nothing loads them."""
    out = tmp_path_factory.mktemp("adas-equivalence-artifacts")
    (out / "adas_reference.so").write_bytes(b"library")
    (out / "AdasReference.fmu").write_bytes(b"archive")
    return out / "adas_reference.so", out / "AdasReference.fmu"


def docs(case, inputs, artifacts, **fmu) -> tuple[dict, dict]:
    library, archive = artifacts
    return (experiment.native_manifest(case, inputs, library).to_doc(),
            experiment.fmu_manifest(case, inputs, archive, **fmu).to_doc())


class TestForms:
    @pytest.mark.parametrize("name", sorted(experiment.CASES))
    def test_only_the_controller_target_differs(self, name, inputs, artifacts):
        case = experiment.CASES[name]
        record = experiment.form_difference(
            case, *docs(case, inputs, artifacts))
        assert {d.split(".")[-1] for d in record["differences"]} <= {
            "type", "library", "config", "command", "step_period_ns",
            "priority"}
        assert record["fmu"]["bind"] == experiment.fmu_bindings(case.maneuver)

    def test_every_degraded_case_of_224_is_covered_or_excluded(self):
        experiments = set(experiment.manifest.EXPERIMENTS)
        covered = {name.split(".", 1)[1] for name in experiment.CASES
                   if "." in name}
        assert covered | set(experiment.OUTSIDE_FMU) == experiments
        assert not covered & set(experiment.OUTSIDE_FMU)
        assert set(experiment.manifest.MANEUVERS) <= set(experiment.CASES)

    def test_another_difference_is_refused(self, inputs, artifacts):
        case = experiment.CASES["cadence"]
        native, fmu = docs(case, inputs, artifacts)
        fmu["channels"]["cadence.radar"]["latency_ns"] = 1
        with pytest.raises(experiment.FormError,
                           match="channels.cadence.radar.latency_ns"):
            experiment.form_difference(case, native, fmu)

    def test_another_parameter_is_refused(self, inputs, artifacts):
        case = experiment.CASES["hazard"]
        native, fmu = docs(case, inputs, artifacts, starts=[
            "hazard_acceleration_mps2=-3.0", "max_change_mps2=1.0"])
        with pytest.raises(experiment.FormError, match="max_change_mps2"):
            experiment.form_difference(case, native, fmu)

    def test_the_bindings_are_the_inspected_mapping(self):
        mapping = json.loads((ROOT / "proofs" / "adas-fmu"
                              / "recorded-input.mapping.json").read_text())
        assert experiment.fmu_bindings("m") == [
            f"m.{bind}" for bind in mapping["bind"]]

    def test_the_cross_form_contract_reads_both_sides_at_the_sample_time(self):
        rule = experiment.cross_form_contract("hazard")["channels"][
            "hazard.command"]
        assert rule["actual_offset_ns"] == rule["reference_offset_ns"] == \
            experiment.PERIOD_NS
        assert rule["fields"]["acceleration_mps2"] == {"atol": 0, "rtol": 0}
        assert rule["fields"]["radar_age_ns"] == "exact"


def _changes(control) -> dict:
    return {key: value for key, value in (("binds", control.binds),
                                          ("starts", control.starts))
            if value is not None}


class TestControls:
    @pytest.mark.parametrize("name", sorted(experiment.CONTROLS))
    def test_a_control_changes_one_thing(self, name, inputs, artifacts):
        control = experiment.CONTROLS[name]
        case = experiment.CASES[control.case]
        _, archive = artifacts
        nominal = experiment.fmu_manifest(case, inputs, archive).to_doc()
        changed_archive = archive.with_name("wrong-sign.fmu") \
            if control.archive_define else archive
        changed = experiment.run_manifest(
            case, inputs,
            experiment.fmu_controller(changed_archive, **_changes(control)),
            input_latency_ns=control.input_latency_ns).to_doc()
        differences = experiment._differences(nominal, changed)
        set_fields = [key for key in ("binds", "starts", "archive_define",
                                      "actual_offset_ns", "input_latency_ns")
                      if getattr(control, key) is not None]
        assert len(set_fields) == 1
        if control.input_latency_ns is not None:
            assert differences == [
                f"channels.{case.maneuver}.{role}.latency_ns"
                for role in ("camera", "ego", "radar")]
        elif control.actual_offset_ns is not None:
            assert differences == []
        else:
            assert differences == [f"participants.{case.maneuver}.command"]

    @pytest.mark.parametrize("name", sorted(experiment.CONTROLS))
    def test_a_prediction_names_an_oracle_value(self, name):
        control = experiment.CONTROLS[name]
        prediction = control.prediction
        with (MANEUVERS / f"{control.case}.expected.csv").open() as f:
            rows = {int(row["time_ms"]) * experiment.MS: row
                    for row in csv.DictReader(f)}
        row = rows[prediction.observation_ns]
        expected = (prediction.observation_ns
                    if prediction.field == "sample_time_ns"
                    else float(row[prediction.field]))
        assert expected == prediction.expected
        assert prediction.actual != prediction.expected


needs_build = pytest.mark.skipif(shutil.which("cc") is None,
                                 reason="needs a C compiler")


@pytest.fixture(scope="module")
def fmu(tmp_path_factory) -> Path:
    """This host's build of the archive."""
    out = tmp_path_factory.mktemp("adas-equivalence-fmu")
    package.build(out)
    return out / "AdasReference.fmu"


@pytest.fixture(scope="module")
def env() -> dict[str, str]:
    """The importer's `python3` is this interpreter, with `sil`."""
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
    env["PYTHONPATH"] = str(ROOT / "python" / "src")
    return env


@needs_build
class TestBothForms:
    """One case through both forms, on this host's build of the archive."""

    def run(self, sil_run, m, path: Path, env) -> Path:
        m.write(path)
        proc = run_manifest(sil_run, path, path.with_suffix(".mcap"), env)
        assert proc.returncode == 0, proc.stderr
        return path.with_suffix(".mcap")

    def test_the_forms_agree_with_the_oracle_and_each_other(
            self, sil_run, build_dir, inputs, fmu, env, tmp_path):
        case = experiment.CASES["cadence.rewrite"]
        native = self.run(sil_run, experiment.native_manifest(
            case, inputs, build_dir / "adas_reference.so"),
            tmp_path / "native.json", env)
        actual = self.run(sil_run, experiment.fmu_manifest(case, inputs, fmu),
                          tmp_path / "fmu.json", env)
        oracle_contract = read_contract(inputs / "cadence.contract.json")
        oracle = inputs / "cadence.rewrite.expected.mcap"
        assert compare(oracle_contract, native, oracle)["verdict"] == "pass"
        assert compare(oracle_contract, actual, oracle)["verdict"] == "pass"
        cross = tmp_path / "cross.json"
        cross.write_text(json.dumps(experiment.cross_form_contract("cadence")))
        assert compare(read_contract(cross), native, actual)["verdict"] == \
            "pass"

    def test_a_control_diverges_where_predicted(
            self, sil_run, inputs, fmu, env, tmp_path):
        control = experiment.CONTROLS["parameter"]
        case = experiment.CASES[control.case]
        recording = self.run(sil_run, experiment.run_manifest(
            case, inputs, experiment.fmu_controller(fmu, starts=control.starts)),
            tmp_path / "control.json", env)
        report = compare(read_contract(inputs / "hazard.contract.json"),
                         recording, inputs / "hazard.expected.mcap")
        first = report["first_divergence"]
        prediction = control.prediction
        assert report["verdict"] == "fail"
        assert (first["observation_ns"], first["field"], first["actual"],
                first["expected"]) == (prediction.observation_ns,
                                       prediction.field, prediction.actual,
                                       prediction.expected)
