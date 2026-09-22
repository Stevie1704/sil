"""Readable table rendering for the machine-readable sensitivity results."""
from __future__ import annotations


def _periods(row: dict) -> str:
    periods = row["periods_ns"]
    return "/".join(
        "-" if periods[name] is None else f"{periods[name] // 1_000_000} ms"
        for name in ("plant", "controller", "kpi")
    )


def _latencies(row: dict) -> str:
    return "/".join(
        "-" if row["latencies_ns"][name] is None
        else f"{row['latencies_ns'][name] // 1_000_000} ms"
        for name in ("sensing", "command", "state")
    )


def markdown_table(results: list[dict]) -> str:
    lines = [
        "| Row | Family | Periods plant/controller/KPI | Latencies sensing/command/state | Reference | Max gap error (m) | Max speed error (m/s) | Max command error (m/s²) | Minimum gap (m) | Baseline Δgap (m) | Envelope |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        row = result["row"]
        errors = result["comparison"]["max_abs_error"]
        envelope = "EXCEEDED: " + ", ".join(result["exceeded_envelope"])
        if not result["exceeded_envelope"]:
            envelope = "within"
        baseline = result.get("sensitivity_comparison")
        baseline_delta = "-" if baseline is None else f"{baseline['minimum_gap_delta_m']:.6g}"
        lines.append(
            f"| {row['name']} | {row['family']} | {_periods(row)} | {_latencies(row)} | "
            f"{row['reference']} | {max(errors.get('gap_m', 0.0), errors.get('ego_position_m', 0.0)):.6g} | "
            f"{errors.get('ego_speed_mps', 0.0):.6g} | {errors.get('accel_mps2', 0.0):.6g} | "
            f"{result['comparison']['minimum_gap_m']:.6g} | {baseline_delta} | {envelope} |"
        )
    return "\n".join(lines) + "\n"


def report_markdown(configuration: dict, results: list[dict], configuration_sha256: str) -> str:
    return "\n".join([
        "# Closed-loop communication-interval sensitivity",
        "",
        "The table reports observed sensitivity; it does not assert monotonic "
        "convergence for a sampled nonlinear controller.",
        "",
        f"Configuration SHA-256: `{configuration_sha256}`",
        "",
        f"Observation policy: exact endpoint times on the {configuration['observation_grid_ns']} ns "
        "grid; no interpolation across discontinuities.",
        "",
        markdown_table(results).rstrip(),
        "",
    ])
