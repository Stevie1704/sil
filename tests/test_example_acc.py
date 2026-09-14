"""The ACC reference example, exercised at the run boundary.

The example is defined once, in `examples/acc/`; this suite imports that one
definition rather than restating it, so a change to the example cannot pass
here while breaking the artifact a reader copies.
"""

import importlib.util
import io
from pathlib import Path

import pytest
from conftest import ROOT

from sil import footprint, schema
from sil.testing import run_simulation


def _load(name: str, path: Path):
    """Import a module that lives outside the importable packages."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


silschema = _load("silschema", ROOT / "tools" / "silschema.py")
manifest = _load("acc_manifest", ROOT / "examples" / "acc" / "manifest.py")
controller = _load("acc_controller", ROOT / "examples" / "acc" / "controller.py")


@pytest.fixture(scope="module")
def acc_result(sil_run, tmp_path_factory):
    """One Run of the example, shared by every assertion made about it.

    The Run is deterministic, so running it once per assertion would only buy
    the same bytes again.
    """
    return run_simulation(
        manifest.acc_manifest(),
        runner=sil_run,
        workdir=tmp_path_factory.mktemp("acc"),
    )


def test_both_ends_of_the_typed_contract_agree_on_the_layout():
    types = schema.load(manifest.ACC_SCHEMAS)
    header = silschema.generate(manifest.ACC_SCHEMAS)
    # Three f64 fields and one, in a packed little-endian layout.
    assert types["acc.Sensing"].size == 24
    assert types["acc.Command"].size == 8
    assert "sizeof(acc_Sensing) == 24" in header
    assert "sizeof(acc_Command) == 8" in header


def test_the_plant_publishes_sensing_every_step(acc_result):
    assert len(acc_result.messages("acc.Sensing")) == (
        manifest.DURATION_NS // manifest.STEP_PERIOD_NS
    )


def test_the_controller_commands_one_step_after_the_sensing_it_answers(
    acc_result,
):
    sensings = acc_result.messages("acc.Sensing")
    commands = acc_result.messages("acc.Command")

    # Under the default Latency the controller first sees sensing one Step
    # after the plant publishes it, so it commands one time fewer.
    assert len(commands) == len(sensings) - 1
    for (sensing_ns, sensing), (command_ns, command) in zip(sensings, commands):
        assert command_ns == sensing_ns + manifest.STEP_PERIOD_NS
        assert command["accel_mps2"] == controller.command_for(**sensing)


def test_the_commanded_acceleration_varies_and_the_ego_closes_the_gap(
    acc_result,
):
    commands = [f["accel_mps2"] for _, f in acc_result.messages("acc.Command")]
    gaps = [f["gap_m"] for _, f in acc_result.messages("acc.Sensing")]

    # A loop that has silently degraded into two participants ignoring each
    # other still publishes both Channels. A command that moves, and a gap
    # that answers it, is what tells the two apart.
    assert len(set(commands)) > 1, "the command never moved, so nothing closed"
    assert gaps[-1] < gaps[0], "the ego never closed the gap it was commanded to"


def test_the_declared_footprint_names_no_unbounded_route():
    doc = manifest.acc_manifest().to_doc()
    out = io.StringIO()

    footprint.report(doc, out)

    # The whole route set, so a route added later without a bound fails here
    # rather than quietly widening the example's declared worst case.
    assert {
        (route.participant, route.channel): route.capacity
        for route in footprint.routes(doc)
    } == {
        ("controller", "acc.Sensing"): manifest.ROUTE_CAPACITY,
        ("plant", "acc.Command"): manifest.ROUTE_CAPACITY,
    }
    assert "unbounded" not in out.getvalue()
