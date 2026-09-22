"""Public Manifest, Test participant and evidence evaluation boundaries."""
import copy

import pytest
from sil.participant import Input
from scenario_contract import FIELDS, NAMES, configuration, manifest
from scenario_participant import ScenarioKPI, Stimulus, violations
from scenarios import compare, post_hoc


def test_authored_identities_and_fault_windows():
    docs = [manifest(configuration(name)).to_doc() for name in NAMES]
    assert len({str(d) for d in docs}) == 4
    for name in NAMES:
        assert manifest(configuration(name)).to_doc() == manifest(configuration(name)).to_doc()
    c = configuration("braking")
    stimulus = Stimulus(c)
    assert [stimulus.on_step(t, 0, [])[0][1]["lead_accel_mps2"]
            for t in (0, 1_990_000_000, 2_000_000_000, 4_990_000_000, 5_000_000_000)] == [0, 0, -4, -4, 0]


def test_changed_gap_fails_after_healthy_initialization():
    c = configuration("dropped")
    kpi = ScenarioKPI(c)
    healthy = dict(zip(FIELDS["truth"], [60, 0, 25, 0, 60, 25]))
    kpi.on_step(10_000_000, 10_000_000, [Input("truth", 0, healthy)])
    unhealthy = {**healthy, "gap_m": 4.9}
    with pytest.raises(RuntimeError, match=r"gap_m publication_ns=6000000000 value=4.9 bounds=\[5.0,inf\]"):
        kpi.on_step(6_010_000_000, 10_000_000, [Input("truth", 6_000_000_000, unhealthy)])


def test_zero_speed_and_final_message_coverage():
    c = configuration("nominal")
    stopped = dict(zip(FIELDS["truth"], [5, 0, 0, 0, 5, 0]))
    assert violations("truth", stopped, 19_990_000_000, c) == []
    actual = {ch: [] for ch in FIELDS}
    actual["truth"] = [(19_990_000_000, [4, 0, 0, 0, 4, 0])]
    assert "publication_ns=19990000000 value=4 bounds=[5.0,inf]" in post_hoc(actual, c)[0]


def test_nonfinite_acceleration_and_tracking_rejected():
    c = configuration("nominal")
    assert violations("command", {"accel_mps2": float("nan")}, 0, c)
    assert violations("command", {"accel_mps2": 1.51}, 0, c)
    moving = dict(zip(FIELDS["truth"], [60, 2, 25, 0, 60, 27]))
    assert not violations("truth", moving, 0, c)
    errors = violations("truth", moving, 15_000_000_000, c)
    assert any("spacing_error_m" in e for e in errors)
    assert any("relative_speed_mps" in e for e in errors)


def test_missing_sensing_holds_delivery_age():
    kpi = ScenarioKPI(configuration("dropped"))
    kpi.on_step(20_000_000, 10_000_000, [Input("sensing", 10_000_000, {})])
    assert kpi.on_step(1_020_000_000, 10_000_000, []) == [("freshness", {"age_ns": 1_000_000_000})]


@pytest.mark.parametrize("fault", ["missing", "nan", "timing", "value"])
def test_reference_comparison_rejects_corrupt_evidence(fault):
    c = configuration("nominal")
    c.update(steps=1, duration_ns=c["step_ns"])
    row = {ch: [0.] * len(names) for ch, names in FIELDS.items()}
    ref = dict(step_ns=c["step_ns"], steps=1, initialization=[60, 0, 25, 0, 60, 25],
               intervals=[dict(slot_ns=0, sensing_publication_ns=0, **row)])
    actual = {ch: [(0, copy.deepcopy(values))] for ch, values in row.items()}
    compare(actual, ref, c)
    if fault == "missing":
        actual["truth"].clear()
    elif fault == "timing":
        actual["truth"][0] = (1, actual["truth"][0][1])
    else:
        actual["truth"][0][1][0] = float("nan") if fault == "nan" else 1
    with pytest.raises(RuntimeError):
        compare(actual, ref, c)
