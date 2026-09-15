"""The FMU import example, exercised at the run boundary.

The example is defined once, in `examples/fmu/`; this suite imports that one
definition rather than restating it, so a change to the example cannot pass
here while breaking the artifact a reader copies.

What the example is for is the mapping: schema field names are FMU variable
names, and `Feedthrough` copies each input to the output of the same name. An
output that equals the input published one Step earlier is therefore the whole
round-trip — write, Step, read, publish — asserted on the Recording.
"""

import importlib.util
from pathlib import Path

import pytest
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
manifest = _load("fmu_manifest", ROOT / "examples" / "fmu" / "manifest.py")


@pytest.fixture(scope="module")
def fmu_result(sil_run, tmp_path_factory):
    """One Run of the example, shared by every assertion made about it."""
    return run_simulation(
        manifest.fmu_manifest(),
        runner=sil_run,
        workdir=tmp_path_factory.mktemp("fmu"),
    )


def test_both_ends_of_the_typed_contract_agree_on_the_layout():
    types = schema.load(manifest.FMU_SCHEMAS)
    header = silschema.generate(manifest.FMU_SCHEMAS)
    # Two f64 fields either way, in a packed little-endian layout.
    assert types["fmu.In"].size == 16
    assert types["fmu.Out"].size == 16
    assert "sizeof(fmu_In) == 16" in header
    assert "sizeof(fmu_Out) == 16" in header


def test_the_stimulus_publishes_every_step(fmu_result):
    assert len(fmu_result.messages("fmu.In")) == (
        manifest.DURATION_NS // manifest.STEP_PERIOD_NS
    )


def test_the_fmu_publishes_every_step(fmu_result):
    assert len(fmu_result.messages("fmu.Out")) == (
        manifest.DURATION_NS // manifest.STEP_PERIOD_NS
    )


def test_the_fmu_echoes_the_channel_it_subscribes_one_step_later(fmu_result):
    """The mapping, end to end, on the artifact a reader copies."""
    inputs = fmu_result.messages("fmu.In")
    outputs = fmu_result.messages("fmu.Out")

    assert [
        (t, fields["Float64_continuous_output"],
         fields["Float64_discrete_output"])
        for t, fields in outputs[1:]
    ] == [
        (t + manifest.STEP_PERIOD_NS, fields["Float64_continuous_input"],
         fields["Float64_discrete_input"])
        for t, fields in inputs[:-1]
    ]


def test_the_stimulus_is_not_constant(fmu_result):
    """A Run that stepped a dead FMU would pass the shape checks on zeros."""
    published = {
        fields["Float64_continuous_output"]
        for _, fields in fmu_result.messages("fmu.Out")
    }
    assert len(published) > 1
