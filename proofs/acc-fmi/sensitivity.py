"""Run the repeatable closed-loop communication-interval sensitivity study."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from loop_contract import validate_archives as validate_closed_loop_archives
from proof_support import (
    PARTICIPANT_TIMEOUT_MS,
    compare_files,
    file_sha256,
    require,
    run_logged,
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
    configuration,
    measured_rows,
    reference_rows,
)
from sensitivity_manifest import manifest_for
from sensitivity_report import report_markdown

HERE = Path(__file__).resolve().parent


def runner_args(path: Path, recording: Path) -> list[str]:
    return [
        "/build/sil-run", str(path), "-o", str(recording),
        "--participant-timeout-ms", str(PARTICIPANT_TIMEOUT_MS),
    ]


def execute(row: dict, out: Path) -> dict:
    """Author twice and run twice, comparing only the same Manifest's bytes."""
    path = out / f"{row['name']}.json"
    manifest_hash = manifest_for(row).write(path).hash
    authored_again = out / f"{row['name']}-authored-again.json"
    manifest_for(row).write(authored_again)
    authored_hashes = compare_files(path, authored_again)
    recordings = [out / f"{row['name']}-{repeat}.mcap" for repeat in (1, 2)]
    for recording in recordings:
        run_logged(
            runner_args(path, recording),
            recording.with_suffix(".log"),
        )
    return {
        "manifest_sha256": manifest_hash,
        "authored_manifest_sha256": authored_hashes,
        "recording_sha256": compare_files(*recordings),
        "recording_files": [path.name for path in recordings],
        "kpi_log": recordings[0].with_suffix(".log").name,
    }


def _reference_tolerance(metrics: dict) -> bool:
    return all(error <= REFERENCE_ABS_TOL for error in metrics["max_abs_error"].values()) and abs(
        metrics["minimum_gap_delta_m"]
    ) <= REFERENCE_ABS_TOL


def _kpi_receipt(log_path: Path) -> dict:
    receipts = []
    for line in log_path.read_text().splitlines():
        if "ACC_SENSITIVITY_KPI " in line:
            receipts.append(json.loads(line.split("ACC_SENSITIVITY_KPI ", 1)[1]))
    require(receipts, f"no sensitivity KPI receipt in {log_path}")
    return {
        "first": receipts[0],
        "last": receipts[-1],
        "activation_receipts": len(receipts),
    }


def _run_reference(row_name: str, config_path: Path, out: Path) -> Path:
    path = out / f"{row_name}.fmpy.json"
    run_logged(
        [sys.executable, str(HERE / "sensitivity_reference.py"), row_name, str(path), str(config_path)],
        path.with_suffix(".log"),
    )
    return path


def _reference_identity(path: Path) -> dict:
    return {"file": path.name, "sha256": file_sha256(path)}


def _validate_reference_initialization(document: dict, config: dict) -> None:
    expected = config["initial_outputs"]
    actual = document["initialization"]
    require(
        actual["state"] == [expected["state"][field] for field in (
            "ego_position_m", "ego_speed_mps", "lead_position_m", "lead_speed_mps",
            "gap_m", "relative_speed_mps",
        )],
        "independent reference initial plant state differs from configuration",
    )
    require(
        actual["sensing"] == [expected["sensing"][field] for field in (
            "gap_m", "relative_speed_mps", "ego_speed_mps",
        )],
        "independent reference initial sensing differs from configuration",
    )
    require(
        actual["command"] == [expected["controller_command_mps2"]],
        "independent reference initial command differs from configuration",
    )


def run(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / "configuration.json"
    authored_configuration = configuration()
    # This is the first experiment artifact written. Every subsequent reference
    # and Run consumes these declared periods, Latencies and acceptance bounds.
    write_json(config_path, authored_configuration)

    capture_environment(out)
    inspect_archives(out)
    validate_closed_loop_archives()
    run_logged(
        [sys.executable, "-m", "pytest", str(HERE / "test_sensitivity.py"), "-q"],
        out / "sensitivity-gate-tests.log",
    )

    refs: dict[str, tuple[dict, Path]] = {}
    for row in reference_rows():
        path = _run_reference(row["name"], config_path, out)
        _validate_reference_initialization(json.loads(path.read_text()), authored_configuration)
        refs[row["name"]] = (row, path)
    for row in measured_rows():
        if row["scenario"] == "changing-input":
            path = _run_reference(row["name"], config_path, out)
            _validate_reference_initialization(json.loads(path.read_text()), authored_configuration)
            refs[row["name"]] = (row, path)

    results = []
    for row in measured_rows():
        run_identity = execute(row, out)
        measured = recording_observations(out / run_identity["recording_files"][0], row)
        sensitivity_comparison = None
        if row["scenario"] == "constant-acceleration":
            target = analytic_observations(row)
            own = target
            reference_identity = {"kind": "analytic-oracle"}
            own_comparison = compare_row(measured, own, constant=True)
            comparison = own_comparison
        else:
            own_row, own_path = refs[row["name"]]
            own = reference_observations(json.loads(own_path.read_text()), own_row)
            own_comparison = compare_row(measured, own)
            target_name = row["reference"]
            target_row, target_path = (
                (row, own_path) if target_name == row["name"] else refs[target_name]
            )
            target = reference_observations(
                json.loads(target_path.read_text()), target_row
            )
            reference_identity = _reference_identity(target_path)
            comparison = compare_row(measured, target)
            if row["sensitivity_baseline"] is not None:
                baseline_row, baseline_path = refs[row["sensitivity_baseline"]]
                baseline = reference_observations(
                    json.loads(baseline_path.read_text()), baseline_row
                )
                sensitivity_comparison = compare_row(measured, baseline)
        require(
            _reference_tolerance(own_comparison),
            f"{row['name']}: SiL differs from its independent reference: {own_comparison}",
        )
        envelope_comparison = sensitivity_comparison or comparison
        exceeded = exceeded_fields(
            envelope_comparison, authored_configuration["acceptance_envelope"]
        )
        if row["negative_control"]:
            require(exceeded, f"{row['name']}: timing defect stayed inside the envelope")
        else:
            require(
                within_envelope(comparison, authored_configuration["acceptance_envelope"]),
                f"{row['name']}: normal sensitivity exceeded the envelope: {comparison}",
            )
            if sensitivity_comparison is not None:
                require(
                    within_envelope(
                        sensitivity_comparison,
                        authored_configuration["acceptance_envelope"],
                    ),
                    f"{row['name']}: baseline sensitivity exceeded the envelope: "
                    f"{sensitivity_comparison}",
                )
        kpi = _kpi_receipt(out / run_identity["kpi_log"]) if row["scenario"] != "constant-acceleration" else None
        results.append({
            "row": row,
            "artifacts": run_identity,
            "reference": reference_identity,
            "independent_match": own_comparison,
            "comparison": comparison,
            "sensitivity_comparison": sensitivity_comparison,
            "exceeded_envelope": exceeded,
            "negative_control_detected": bool(exceeded) if row["negative_control"] else False,
            "in_run_kpi": kpi,
        })

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
        "determinism_policy": (
            "Manifest and Recording bytes are compared only within the same row; "
            "different step-size MCAP files are never compared for byte equality."
        ),
    }
    write_json(out / "sensitivity-report.json", report)
    write_json(out / "results.json", {"rows": results})
    (out / "sensitivity-report.md").write_text(
        report_markdown(authored_configuration, results, file_sha256(config_path))
    )


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
