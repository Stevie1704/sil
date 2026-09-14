"""The ACC reference example, exercised at the run boundary.

The example is defined once, in `examples/acc/`; this suite imports that one
definition rather than restating it, so a change to the example cannot pass
here while breaking the artifact a reader copies.
"""

import importlib.util
from pathlib import Path

from conftest import ROOT

from sil import schema
from sil.testing import run_simulation


def _load(name: str, path: Path):
    """Import a module that lives outside the importable packages."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


silschema = _load("silschema", ROOT / "tools" / "silschema.py")
manifest = _load("acc_manifest", ROOT / "examples" / "acc" / "manifest.py")
plant = _load("acc_plant", ROOT / "examples" / "acc" / "plant.py")


def test_both_ends_of_the_typed_contract_agree_on_the_layout():
    types = schema.load(manifest.ACC_SCHEMAS)
    header = silschema.generate(manifest.ACC_SCHEMAS)
    # Three f64 fields and one, in a packed little-endian layout.
    assert types["acc.Sensing"].size == 24
    assert types["acc.Command"].size == 8
    assert "sizeof(acc_Sensing) == 24" in header
    assert "sizeof(acc_Command) == 8" in header


def _gaps(result):
    return [fields["gap_m"] for _, fields in result.messages("acc.Sensing")]


def test_the_plant_publishes_sensing_every_step(sil_run, tmp_path):
    result = run_simulation(
        manifest.acc_manifest(), runner=sil_run, workdir=tmp_path
    )
    assert len(_gaps(result)) == (
        manifest.DURATION_NS // manifest.PLANT_STEP_PERIOD_NS
    )


def test_the_gap_moves_the_way_the_fixed_command_implies(sil_run, tmp_path):
    result = run_simulation(
        manifest.acc_manifest(), runner=sil_run, workdir=tmp_path
    )
    gaps = _gaps(result)
    # Both vehicles start at the same speed, so the gap's whole motion comes
    # from the acceleration difference: the ego pulls in when it is commanded
    # to out-accelerate the lead, and drops back when it is not.
    assert plant.EGO_SPEED_MPS == plant.LEAD_SPEED_MPS, (
        "the direction below is only implied by the command while the two "
        "vehicles start at the same speed"
    )
    closing = plant.COMMANDED_ACCEL_MPS2 > plant.LEAD_ACCEL_MPS2
    steps = list(zip(gaps, gaps[1:]))
    assert steps, "the run recorded too few messages to show motion"
    assert all(
        (later < earlier) if closing else (later > earlier)
        for earlier, later in steps
    )
