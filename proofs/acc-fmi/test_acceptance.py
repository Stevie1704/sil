"""Acceptance-bundle gates must reject tampering and swallowed failures."""
import json

import pytest
from acceptance_contract import (
    DECLARED_CO_SIMULATION,
    EXERCISED_SYMBOLS,
    MODELS,
    SCENARIO_CHECKS,
    SENSITIVITY_CHECKS,
    fmi_profile,
    handoff,
    reference_names,
    sensitivity_row,
)
import acceptance_bundle
from acceptance_verdict import (
    determinism_summary,
    envelope_verdict,
    expected_failure_verdict,
    require_minimal_runtime,
)


def audit():
    return {
        model: {
            "archive_sha256": "0" * 64,
            "schema_valid": True,
            "capabilities": {"modelIdentifier": model, **DECLARED_CO_SIMULATION},
            "exported_symbols": list(EXERCISED_SYMBOLS),
        }
        for model in MODELS
    }


def bundle(tmp_path):
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "latency-10ms.json").write_text("{}\n")
    (tmp_path / "archives.json").write_text(json.dumps(audit()))
    acceptance_bundle.write_index(tmp_path, {"prepared_by": "test"})
    return tmp_path


def test_prepared_bundle_verifies(tmp_path):
    root = bundle(tmp_path)
    assert acceptance_bundle.verify(root)["prepared_by"] == "test"


@pytest.mark.parametrize("damage", ["alter", "remove", "add"])
def test_tampered_bundle_is_rejected(tmp_path, damage):
    root = bundle(tmp_path)
    reference = root / "references" / "latency-10ms.json"
    if damage == "alter":
        reference.write_text('{"messages": {}}\n')
    elif damage == "remove":
        reference.unlink()
    else:
        (root / "references" / "extra.json").write_text("{}\n")
    with pytest.raises(RuntimeError, match="bundle"):
        acceptance_bundle.verify(root)


def test_profile_rejects_a_changed_declared_capability():
    archives = audit()
    archives["AccPlant"]["capabilities"]["canGetAndSetFMUState"] = "true"
    with pytest.raises(RuntimeError, match="canGetAndSetFMUState"):
        fmi_profile(archives)


def test_profile_rejects_a_missing_exercised_symbol():
    archives = audit()
    archives["AccController"]["exported_symbols"] = ["fmi3DoStep"]
    with pytest.raises(RuntimeError, match="fmi3InstantiateCoSimulation"):
        fmi_profile(archives)


def test_profile_names_the_rejected_capabilities():
    profile = fmi_profile(audit())
    rejected = {entry["capability"] for entry in profile["rejected"]}
    assert "canHandleVariableCommunicationStepSize" in rejected
    assert "Model Exchange and Scheduled Execution interfaces" in profile["not_exercised"]


def test_every_check_pins_the_references_it_compares_against():
    names = reference_names()
    for check in SENSITIVITY_CHECKS:
        row = sensitivity_row(check)
        assert row.name in names and row.reference in names
        assert row.sensitivity_reference in names


def test_the_negative_control_must_exceed_the_envelope():
    envelope = {"gap_m": 1.0, "minimum_gap_m": 1.0}
    inside = {"max_abs_error": {"gap_m": 0.5}, "minimum_gap_delta_m": 0.0}
    outside = {"max_abs_error": {"gap_m": 2.0}, "minimum_gap_delta_m": 0.0}

    assert envelope_verdict(sensitivity_row("latency-20ms"), inside, envelope)["within"]
    with pytest.raises(RuntimeError, match="exceeded"):
        envelope_verdict(sensitivity_row("latency-20ms"), outside, envelope)

    assert envelope_verdict(sensitivity_row("timing-defect"), outside, envelope)["detected"]
    with pytest.raises(RuntimeError, match="stayed inside"):
        envelope_verdict(sensitivity_row("timing-defect"), inside, envelope)


def test_an_expected_failure_is_asserted_rather_than_swallowed():
    verdict = expected_failure_verdict("dropped", 1, ["ACC KPI gap_m publication_ns=7 value=4"])
    assert verdict["expected_exit"] == 1 and verdict["diagnostic"].startswith("ACC KPI gap_m")

    with pytest.raises(RuntimeError, match="succeeded"):
        expected_failure_verdict("dropped", 0, [])
    with pytest.raises(RuntimeError, match="without a post-hoc diagnostic"):
        expected_failure_verdict("dropped", 1, [])


def test_a_toolchain_or_comparison_tool_in_the_example_image_fails():
    require_minimal_runtime(present_modules=(), present_tools=())
    with pytest.raises(RuntimeError, match="fmpy"):
        require_minimal_runtime(present_modules=("fmpy",), present_tools=())
    with pytest.raises(RuntimeError, match="cmake"):
        require_minimal_runtime(present_modules=(), present_tools=("cmake",))


def test_handoff_carries_the_signals_and_kpis_the_scenarios_enforce():
    document = handoff()
    channels = {signal["channel"] for signal in document["signals"]}
    assert {"sensing", "truth", "command", "maneuver", "freshness"} == channels
    assert document["kpis"]["minimum_gap_m"] == 5.0
    assert all(signal["rate_hz"] for signal in document["signals"])
    assert set(SCENARIO_CHECKS) == {"nominal", "dropped"}


def check_result(manifest, recordings):
    return {"artifacts": {"manifest_sha256": manifest,
                          "authored_manifest_sha256": [manifest, manifest],
                          "recording_sha256": recordings}}


def test_per_manifest_determinism_is_judged_within_one_check():
    results = {"scenarios": {"nominal": check_result("a", ["r", "r"])},
               "sensitivity": {"latency-20ms": check_result("b", ["s", "s"])}}
    assert set(determinism_summary(results)) == {"nominal", "latency-20ms"}

    repeated = {"scenarios": {"nominal": check_result("a", ["r", "r"]),
                              "dropped": check_result("a", ["r", "r"])}}
    with pytest.raises(RuntimeError, match="share one Manifest identity"):
        determinism_summary(repeated)

    differing = {"scenarios": {"nominal": check_result("a", ["r", "other"])}}
    with pytest.raises(RuntimeError, match="different Recordings"):
        determinism_summary(differing)
