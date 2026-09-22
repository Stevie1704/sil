"""Run the authored closed-loop communication-period sensitivity study."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from loop_contract import validate_archives as validate_closed_loop_archives
from proof_support import (
    file_sha256,
    require,
    run_logged,
    run_manifest_twice,
    write_json,
)
from qualify import capture_environment, inspect_archives
from sensitivity_compare import (
    analytic_observations,
    compare_row,
    exceeded_fields,
    recording_observations,
    reference_observations,
    within_envelope,
)
from sensitivity_contract import (
    REFERENCE_ABS_TOL,
    SensitivityRow,
    configuration,
    measured_rows,
    reference_rows,
)
from sensitivity_evidence import retain, write_plots
from sensitivity_manifest import manifest_for
from sensitivity_report import report_markdown

HERE = Path(__file__).resolve().parent


def _reference_tolerance(metrics: dict) -> bool:
    return (
        all(
            error <= REFERENCE_ABS_TOL
            for error in metrics["max_abs_error"].values()
        )
        and abs(metrics["minimum_gap_delta_m"]) <= REFERENCE_ABS_TOL
    )


def _kpi_receipt(log_path: Path) -> dict:
    receipts = [
        json.loads(line.split("ACC_SENSITIVITY_KPI ", 1)[1])
        for line in log_path.read_text().splitlines()
        if "ACC_SENSITIVITY_KPI " in line
    ]
    require(receipts, f"no sensitivity KPI receipt in {log_path}")
    require(len(receipts) == 1, f"KPI receipt was emitted more than once: {log_path}")
    return receipts[0]


def _run_reference(row: SensitivityRow, config_path: Path, out: Path) -> Path:
    path = out / f"{row.name}.fmpy.json"
    run_logged(
        [
            sys.executable,
            str(HERE / "sensitivity_reference.py"),
            row.name,
            str(path),
            str(config_path),
        ],
        path.with_suffix(".log"),
    )
    return path


def _reference_identity(path: Path) -> dict:
    return {"file": path.name, "sha256": file_sha256(path)}


def _validate_reference_initialization(
    document: dict, config: dict, row: SensitivityRow,
) -> None:
    def matches(actual: list[float], expected: list[float]) -> bool:
        return len(actual) == len(expected) and all(
            math.isclose(value, wanted, rel_tol=0.0, abs_tol=REFERENCE_ABS_TOL)
            for value, wanted in zip(actual, expected)
        )

    expected = config["initial_outputs"]
    actual = document["initialization"]
    expected_state = [
        expected["state"][field]
        for field in (
            "ego_position_m", "ego_speed_mps", "lead_position_m",
            "lead_speed_mps", "gap_m", "relative_speed_mps",
        )
    ]
    require(
        matches(actual["state"], expected_state),
        f"{row.name}: independent reference initial plant state differs from configuration",
    )
    if row.closed_loop:
        require(
            matches(actual["sensing"], [
                expected["sensing"][field]
                for field in ("gap_m", "relative_speed_mps", "ego_speed_mps")
            ]),
            f"{row.name}: independent reference initial sensing differs from configuration",
        )
        require(
            matches(actual["command"], [expected["controller_command_mps2"]]),
            f"{row.name}: independent reference initial command differs from configuration",
        )
    else:
        require(
            set(actual) == {"state"},
            f"{row.name}: plant-only reference unexpectedly initializes closed-loop channels",
        )


def _reference_rows() -> tuple[SensitivityRow, ...]:
    """Return every row requiring an independent FMPy trajectory."""
    return tuple(reference_rows()) + tuple(
        row for row in measured_rows() if row.closed_loop
    )


def _analytic_reference(row: SensitivityRow) -> dict:
    return {
        "kind": "analytic-oracle",
        "profile": "constant-acceleration"
        if row.kind == "constant-acceleration"
        else "piecewise-constant-lead-acceleration",
    }


def run(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / "configuration.json"
    authored_configuration = configuration()
    # Configuration is the first experiment artifact. References and Runs only
    # consume periods, latencies, profile and acceptance bounds from this file.
    write_json(config_path, authored_configuration)

    capture_environment(out)
    inspect_archives(out)
    validate_closed_loop_archives(require_sensitivity_inputs=True)
    run_logged(
        [sys.executable, "-m", "pytest", str(HERE / "test_sensitivity.py"), "-q"],
        out / "sensitivity-gate-tests.log",
    )

    references: dict[str, tuple[SensitivityRow, Path, dict]] = {}
    for row in _reference_rows():
        path = _run_reference(row, config_path, out)
        document = json.loads(path.read_text())
        _validate_reference_initialization(document, authored_configuration, row)
        references[row.name] = (row, path, document)

    results = []
    for row in measured_rows():
        run_identity = run_manifest_twice(
            lambda: manifest_for(row), row.name, out,
        )
        measured = recording_observations(
            out / run_identity["recording_files"][0], row,
        )

        if not row.closed_loop and row.kind in {
            "constant-acceleration", "changing-input",
        }:
            target = analytic_observations(row)
            comparison = compare_row(measured, target, row)
            own_comparison = comparison
            reference_identity = _analytic_reference(row)
            # The constant path is an exact FMU sanity oracle. The forcing path
            # intentionally measures held-input discretization against the
            # continuous piecewise-input oracle, so it is not required to be
            # bit-close to that oracle.
            if row.kind == "constant-acceleration":
                require(
                    _reference_tolerance(own_comparison),
                    f"{row.name}: constant FMU differs from analytic oracle: {own_comparison}",
                )
        else:
            own_row, _own_path, own_document = references[row.name]
            own = reference_observations(own_document, own_row)
            own_comparison = compare_row(measured, own, row)
            require(
                _reference_tolerance(own_comparison),
                f"{row.name}: SiL differs from its independent reference: {own_comparison}",
            )

            target_row, target_path, target_document = references[row.reference]
            target = reference_observations(target_document, target_row)
            reference_identity = _reference_identity(target_path)
            comparison = compare_row(measured, target, row)

        sensitivity_comparison = None
        if row.sensitivity_reference is not None:
            sensitivity_reference_row, _reference_path, reference_document = references[
                row.sensitivity_reference
            ]
            sensitivity_reference = reference_observations(
                reference_document, sensitivity_reference_row,
            )
            sensitivity_comparison = compare_row(
                measured, sensitivity_reference, row,
            )

        envelope_comparison = sensitivity_comparison or comparison
        exceeded = exceeded_fields(
            envelope_comparison,
            authored_configuration["acceptance_envelope"],
        )
        if row.negative_control:
            require(
                exceeded,
                f"{row.name}: timing defect stayed inside the declared envelope",
            )
        else:
            require(
                within_envelope(
                    comparison, authored_configuration["acceptance_envelope"],
                ),
                f"{row.name}: sensitivity exceeded the declared envelope: {comparison}",
            )
            if sensitivity_comparison is not None:
                require(
                    within_envelope(
                        sensitivity_comparison,
                        authored_configuration["acceptance_envelope"],
                    ),
                    f"{row.name}: reference sensitivity exceeded the declared envelope: "
                    f"{sensitivity_comparison}",
                )

        results.append({
            "row": row.to_document(),
            "artifacts": run_identity,
            "reference": reference_identity,
            "independent_match": own_comparison,
            "comparison": comparison,
            "sensitivity_comparison": sensitivity_comparison,
            "exceeded_envelope": exceeded,
            "negative_control_detected": bool(exceeded) if row.negative_control else False,
            "in_run_kpi": _kpi_receipt(out / run_identity["kpi_log"])
            if row.closed_loop else None,
        })

    forcing_results = [
        result for result in results
        if result["row"]["family"] == "plant-forcing-oracle"
    ]
    require(
        any(
            result["comparison"]["max_abs_error"].get("lead_position_m", 0.0) > 1e-9
            for result in forcing_results
        ),
        "piecewise-input forcing produced no measurable plant-period sensitivity",
    )

    report = {
        "experiment": authored_configuration["experiment"],
        "configuration_sha256": file_sha256(config_path),
        "configuration": authored_configuration,
        "environment": json.loads((out / "environment.json").read_text()),
        "fmus": {
            name: json.loads((out / f"{name}.identity.json").read_text())
            for name in ("AccController", "AccPlant")
        },
        "results": results,
        "plots": write_plots(out, results),
        "determinism_policy": (
            "Manifest and Recording bytes are compared only within the same row; "
            "different periods and different Maneuver traces are never compared "
            "for byte equality."
        ),
    }
    write_json(out / "sensitivity-report.json", report)
    write_json(out / "results.json", {"rows": results})
    (out / "sensitivity-report.md").write_text(
        report_markdown(
            authored_configuration, results, file_sha256(config_path), report["plots"],
        )
    )
    retain(out, out / "curated")


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
