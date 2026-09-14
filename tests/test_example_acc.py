"""The ACC reference example, exercised at the run boundary.

The example is defined once, in `examples/acc/`; this suite imports that one
definition rather than restating it, so a change to the example cannot pass
here while breaking the artifact a reader copies.
"""

import importlib.util
import io
from pathlib import Path

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


def test_both_ends_of_the_typed_contract_agree_on_the_layout():
    types = schema.load(manifest.ACC_SCHEMAS)
    header = silschema.generate(manifest.ACC_SCHEMAS)
    # Three f64 fields and one, in a packed little-endian layout.
    assert types["acc.Sensing"].size == 24
    assert types["acc.Command"].size == 8
    assert "sizeof(acc_Sensing) == 24" in header
    assert "sizeof(acc_Command) == 8" in header


def _sensings(result):
    return result.messages("acc.Sensing")


def _commands(result):
    return result.messages("acc.Command")


def test_the_plant_publishes_sensing_every_step(sil_run, tmp_path):
    result = run_simulation(
        manifest.acc_manifest(), runner=sil_run, workdir=tmp_path
    )
    assert len(_sensings(result)) == (
        manifest.DURATION_NS // manifest.STEP_PERIOD_NS
    )


def test_the_controller_commands_one_step_after_the_sensing_it_answers(
    sil_run, tmp_path
):
    result = run_simulation(
        manifest.acc_manifest(), runner=sil_run, workdir=tmp_path
    )
    sensings = _sensings(result)
    commands = _commands(result)
    # Under the default unit latency the controller first sees sensing one
    # Step after the plant publishes it, so it commands one time fewer.
    assert len(commands) == len(sensings) - 1
    for (sensing_ns, sensing), (command_ns, command) in zip(sensings, commands):
        assert command_ns == sensing_ns + manifest.STEP_PERIOD_NS
        assert command["accel_mps2"] == controller.command_for(**sensing)


def test_the_commanded_acceleration_moves_with_the_measured_gap(
    sil_run, tmp_path
):
    result = run_simulation(
        manifest.acc_manifest(), runner=sil_run, workdir=tmp_path
    )
    commands = [fields["accel_mps2"] for _, fields in _commands(result)]
    # A loop that has silently degraded into two participants ignoring each
    # other still publishes both Channels; a command that answers the gap is
    # what tells the two apart.
    assert len(set(commands)) > 1, "the command never moved, so nothing closed"
    gaps = [fields["gap_m"] for _, fields in _sensings(result)]
    assert gaps[-1] < gaps[0], "the ego never closed the gap it was commanded to"


def test_the_declared_footprint_is_finite_and_names_no_unbounded_route():
    doc = manifest.acc_manifest().to_doc()
    routes = footprint.routes(doc)
    out = io.StringIO()
    footprint.report(doc, out)

    assert routes, "the example declares no subscriber route to bound"
    assert all(route.capacity is not None for route in routes)
    assert sum(route.total_bytes for route in routes) > 0
    assert "unbounded" not in out.getvalue()
