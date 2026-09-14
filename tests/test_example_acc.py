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
from sil.testing import RunFailure, run_simulation


def _load(name: str, path: Path):
    """Import a module that lives outside the importable packages."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


silschema = _load("silschema", ROOT / "tools" / "silschema.py")
manifest = _load("acc_manifest", ROOT / "examples" / "acc" / "manifest.py")
controller = _load("acc_controller", ROOT / "examples" / "acc" / "controller.py")
safety = _load("acc_safety", ROOT / "examples" / "acc" / "safety.py")


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


@pytest.fixture(scope="module")
def delayed_sensing_result(sil_run, tmp_path_factory):
    """The same Run with the sensing Channel's delay Interceptor declared."""
    return run_simulation(
        manifest.acc_manifest(delayed_sensing=True),
        runner=sil_run,
        workdir=tmp_path_factory.mktemp("acc-delayed"),
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


def test_the_recorded_run_holds_the_safety_gap(acc_result):
    """The post-hoc half of the KPI the test participant asserts in-run.

    The fixture reaching a Recording at all is the nominal Run's exit 0:
    `run_simulation` raises on any other exit code. What is left to compute is
    the KPI itself — a comprehension over the typed Messages, with no
    evaluation machinery between the Recording and the assertion.
    """
    gaps_m = [fields["gap_m"] for _, fields in acc_result.messages("acc.Sensing")]

    assert min(gaps_m) >= safety.SAFE_GAP_M


def test_an_unmeetable_safety_gap_aborts_the_run_with_its_own_message(
    sil_run, tmp_path,
):
    """The in-run assertion is load-bearing, not decorative.

    Same example, same plant and controller; only the threshold the test
    participant holds them to changes. What the Run cannot meet has to abort
    it, and the participant's own words have to be what a reader sees.
    """
    with pytest.raises(RunFailure, match="safety gap") as failure:
        run_simulation(
            manifest.acc_manifest(safety_kpi="UnmeetableMinimumGapKPI"),
            runner=sil_run,
            workdir=tmp_path,
        )

    # The kernel's run-failure exit code (kExitRunFailure in kernel/src/main.cpp),
    # as distinct from the exit 2 a Manifest the loader rejects would give.
    assert failure.value.exit_code == 1


def test_the_delayed_variant_differs_by_the_one_declared_interceptor():
    """The two variants are one Manifest apart, and the hashes say so.

    A reader comparing the delayed Run against the nominal one needs more than
    two hashes that differ: two hashes only say the Runs are not the same Run.
    Taking the Interceptor back out of the delayed document has to leave the
    nominal one exactly, and that is what says nothing else moved.
    """
    nominal_builder = manifest.acc_manifest()
    delayed_builder = manifest.acc_manifest(delayed_sensing=True)
    assert delayed_builder.hash() != nominal_builder.hash()

    delayed = delayed_builder.to_doc()
    declared = delayed["channels"]["acc.Sensing"].pop("interceptors")

    assert declared == [
        {
            "kind": "delay",
            "delay_ns": manifest.SENSING_DELAY_NS,
            "start_ns": manifest.SENSING_DELAY_START_NS,
            "end_ns": manifest.SENSING_DELAY_END_NS,
        }
    ]
    assert delayed == nominal_builder.to_doc()


def test_the_delayed_variant_drives_a_different_trajectory(
    acc_result, delayed_sensing_result,
):
    """The Interceptor's effect is shown on the vehicles, not asserted to exist.

    Both Runs are the same plant and the same control law. Only when the
    controller sees the gap differs, so every metre between the two
    trajectories was put there by the declared delay.
    """
    nominal_gaps_m = [f["gap_m"] for _, f in acc_result.messages("acc.Sensing")]
    delayed_gaps_m = [
        f["gap_m"] for _, f in delayed_sensing_result.messages("acc.Sensing")
    ]

    # The Interceptor shifts when a Message becomes visible, not whether it
    # exists, so both Runs publish one sensing Message per Step and an index
    # into either list is the same plant Step.
    assert len(delayed_gaps_m) == len(nominal_gaps_m)

    # Nothing can differ before the window opens.
    window_start = manifest.SENSING_DELAY_START_NS // manifest.STEP_PERIOD_NS
    assert delayed_gaps_m[:window_start] == nominal_gaps_m[:window_start]

    # A centimetre is far above the last bits of a double and is a distance a
    # reader can picture, so a delay that changed only rounding fails here.
    divergence_m = [abs(d - n) for d, n in zip(delayed_gaps_m, nominal_gaps_m)]
    assert max(divergence_m) > 0.01, "the declared delay changed nothing"

    # Late sensing is late braking: the delayed ego ends the Run nearer the
    # lead vehicle than the nominal one, which is the direction the delay has.
    assert delayed_gaps_m[-1] < nominal_gaps_m[-1]


@pytest.mark.parametrize("delayed_sensing", [False, True])
def test_the_declared_footprint_names_no_unbounded_route(delayed_sensing):
    doc = manifest.acc_manifest(delayed_sensing=delayed_sensing).to_doc()
    out = io.StringIO()

    footprint.report(doc, out)

    # The whole route set, so a route added later without a bound fails here
    # rather than quietly widening the example's declared worst case. Both
    # variants declare the same routes: an Interceptor is a property of a
    # Channel, and the route depth it builds is what the sensing capacity
    # covers.
    assert {
        (route.participant, route.channel): route.capacity
        for route in footprint.routes(doc)
    } == {
        ("controller", "acc.Sensing"): manifest.SENSING_ROUTE_CAPACITY,
        ("plant", "acc.Command"): manifest.ROUTE_CAPACITY,
        ("test", "acc.Sensing"): manifest.SENSING_ROUTE_CAPACITY,
    }
    assert "unbounded" not in out.getvalue()
