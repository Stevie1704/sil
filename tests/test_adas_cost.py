"""The reference execution-cost measurement of the ADAS controller (#229).

`proofs/adas-cost/` measures one declared workload in three execution forms
on Linux x86-64 and retains the results. These tests check the declaration,
the Manifests of the forms, the estimate arithmetic and, on this host, one
short end-to-end measurement against a staged installation, including that
its equivalence check can fail.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from conftest import ROOT

PROOF_DIR = ROOT / "proofs" / "adas-cost"
sys.path.insert(0, str(PROOF_DIR))
try:
    import measure
    import workload
finally:
    sys.path.remove(str(PROOF_DIR))

MS = 1_000_000


@pytest.fixture(scope="module")
def ci_inputs(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("adas-cost-inputs")
    workload.write_inputs(workload.workloads()["ci"], 2, out)
    return out


class TestDeclaration:
    def test_the_matrix_covers_every_form_run_instance_and_recording(self):
        rows = workload.matrix()
        assert len(rows) == len({r.name for r in rows}) == 3 * 3 * 2 * 2
        assert {r.form for r in rows} == set(workload.FORMS)
        assert {r.workload.name for r in rows} == {"startup", "ci", "long"}

    def test_the_runs_are_one_activation_200_ms_and_the_long_duration(self):
        runs = workload.workloads(long_s=60)
        assert [w.duration_ns for w in runs.values()] == [
            10 * MS, 200 * MS, 60_000 * MS]

    def test_every_issue_dimension_is_pinned(self):
        d = workload.declaration()
        for key in ("controller_period_ns", "sensor_periods_ns",
                    "list_capacity", "active_objects", "message_bytes",
                    "route_capacity", "fan_out", "workloads", "instances",
                    "recording", "policy"):
            assert key in d, key
        assert d["message_bytes"] == {"adas.ObjectList": 185,
                                      "adas.EgoMotion": 17,
                                      "adas.Command": 56}
        assert d["policy"]["warmup_runs"] >= 1
        assert d["policy"]["timed_repeats"] >= 3


class TestInputs:
    def test_each_sensor_publishes_full_lists_at_its_own_period(
            self, ci_inputs):
        with (ci_inputs / "load0.inputs.csv").open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 20
        for sensor, period_ms in (("radar", 20), ("camera", 40), ("ego", 10)):
            times = [int(r["time_ms"]) for r in rows if r[f"{sensor}_t_ns"]]
            assert times == list(range(0, 200, period_ms)), sensor
        assert {r["radar_count"] for r in rows if r["radar_t_ns"]} == {"8"}
        assert {r["camera_count"] for r in rows if r["camera_t_ns"]} == {"8"}

    def test_every_instance_replays_the_same_maneuver_under_its_prefix(
            self, ci_inputs):
        first = (ci_inputs / "load0.inputs.csv").read_text()
        assert (ci_inputs / "load1.inputs.csv").read_text() == first
        mapping = json.loads(
            (ci_inputs / "load1.inputs.mapping.json").read_text())
        assert {c["channel"] for c in mapping["channels"]} == {
            "load1.radar", "load1.camera", "load1.ego"}

    def test_the_lead_object_enters_the_hazard_distance_every_cycle(self):
        rows = workload.authored_rows(workload.CYCLE_ACTIVATIONS)
        leads = [float(r["radar"].split(";")[0].split()[1])
                 for r in rows if r["radar"]]
        assert max(leads) == 50.0 and 0 < min(leads) < 8.0


class TestForms:
    def docs(self, ci_inputs, instances=2):
        artifacts = workload.Artifacts(Path("/lib/adas_reference.so"),
                                       Path("/lib/AdasReference.fmu"))
        ci = workload.workloads()["ci"]
        return {form: workload.row_manifest(
            workload.Row(form, ci, instances, True), ci_inputs,
            artifacts).to_doc() for form in workload.FORMS}

    def test_only_the_controller_entries_differ(self, ci_inputs):
        docs = self.docs(ci_inputs)
        workload.only_the_controller_differs(docs, 2)
        kinds = {form: doc["participants"]["load1"]["type"]
                 for form, doc in docs.items()}
        assert kinds == {"native": "native", "process": "process",
                         "fmu": "process"}

    def test_another_difference_is_refused(self, ci_inputs):
        docs = self.docs(ci_inputs)
        docs["fmu"]["channels"]["load0.radar"]["latency_ns"] = 1
        with pytest.raises(workload.FormError, match="fmu"):
            workload.only_the_controller_differs(docs, 2)


def _row(form, run, n, recording, median, spread=0.0):
    wall = {"median": median, "min": median - spread / 2,
            "max": median + spread / 2}
    return {"form": form, "workload": run, "instances": n,
            "recording": recording, "observational": {"wall_s": wall}}


class TestEstimates:
    def results(self, spread=0.0):
        rows = []
        for form in workload.FORMS:
            for n in workload.INSTANCES:
                for recording in (False, True):
                    extra = 0.001 * n if recording else 0
                    rows += [_row(form, "startup", n, recording, 0.1, spread),
                             _row(form, "ci", n, recording, 0.2, spread),
                             _row(form, "long", n, recording,
                                  0.2 + 0.00002 * 5980 * n + extra, spread)]
        return rows

    def test_the_per_step_cost_nets_the_shorter_run_and_the_application(self):
        application = {"long": {"us_per_activation": {"median": 5.0}}}
        e = measure.estimates(self.results(), application, long_s=60)
        native = e["native-x4"]
        assert native["participant_step"]["us"] == pytest.approx(20.0)
        assert native["adaptation_and_routing_us_per_step"] == \
            pytest.approx(15.0)
        assert native["recording_per_participant_step"]["us"] == \
            pytest.approx(0.004 / (6000 * 4) * 1e6)
        assert native["participant_step"]["resolved"]

    def test_a_difference_inside_the_spread_is_not_resolved(self):
        application = {"long": {"us_per_activation": {"median": 5.0}}}
        e = measure.estimates(self.results(spread=1.0), application,
                              long_s=60)
        assert not e["fmu-x1"]["participant_step"]["resolved"]
        assert not e["fmu-x1"]["recording_per_participant_step"]["resolved"]


needs_cc = pytest.mark.skipif(shutil.which("cc") is None,
                              reason="needs a C compiler")


@pytest.fixture(scope="module")
def measured(staged_prefix, build_dir, tmp_path_factory):
    """A short measurement against a copy of the staged installation, with
    the instrumented runner beside the production one."""
    prefix = tmp_path_factory.mktemp("adas-cost-prefix") / "prefix"
    shutil.copytree(staged_prefix, prefix, symlinks=True)
    shutil.copy2(build_dir / "sil-run-instrumented",
                 prefix / "bin" / "sil-run-instrumented")
    out = tmp_path_factory.mktemp("adas-cost")
    path = os.pathsep.join([str(prefix / "bin"),
                            str(Path(sys.executable).parent),
                            os.environ["PATH"]])
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", path)
        yield out, measure.measure(out, long_s=1, warmup=0, repeats=2)


@needs_cc
class TestMeasurement:
    def test_every_check_passes(self, measured):
        _, result = measured
        failed = {k: c["detail"] for k, c in result["checks"].items()
                  if not c["passed"]}
        assert not failed
        assert result["passed"]

    def test_the_results_keep_counters_apart_from_timings(self, measured):
        out, result = measured
        written = json.loads((out / "results.json").read_text())
        assert written["passed"]
        row = written["rows"][0]
        assert set(row["deterministic"]) == {
            "exit_codes", "counters", "counters_repeat",
            "recording_equals_instrumented"}
        assert "wall_s" in row["observational"]
        assert "## Estimates" in (out / "results.md").read_text()

    def test_every_form_crosses_its_own_boundary(self, measured):
        _, result = measured
        counters = {r["name"]: r["deterministic"]["counters"]
                    for r in result["rows"]}
        assert counters["native-ci-x1-recon"]["inline_encode"]["count"] == 0
        for form in ("process", "fmu"):
            assert counters[f"{form}-ci-x1-recon"]["inline_decode"][
                "count"] == 20

    def test_a_form_that_publishes_other_commands_fails_the_check(
            self, measured):
        out, _ = measured
        runs = out / "work" / "runs"
        shutil.copy2(runs / "native-startup-x1-recon" / "instrumented-1.mcap",
                     runs / "process-ci-x1-recon" / "instrumented-1.mcap")
        m = measure.Measurement(out, "cc", 1, 0, 2)
        m.equivalence(workload.matrix(long_s=1))
        check = m.checks["forms_publish_identical_commands"]
        assert not check["passed"]
        assert check["detail"] == [
            "process-ci-x1-recon differs from native-ci-x1-recon"]
