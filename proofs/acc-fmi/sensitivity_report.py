"""Readable table rendering for machine-readable sensitivity results."""
from __future__ import annotations


def _periods(row: dict) -> str:
    periods = row["periods_ns"]
    return "/".join(
        "-" if periods[name] is None else f"{periods[name] // 1_000_000} ms"
        for name in ("plant", "controller", "maneuver", "kpi")
    )


def _latencies(row: dict) -> str:
    latencies = row["latencies_ns"]
    return "/".join(
        "-" if latencies[name] is None
        else f"{latencies[name] // 1_000_000} ms"
        for name in ("sensing", "command", "maneuver", "state")
    )


def markdown_table(results: list[dict]) -> str:
    lines = [
        "| Row | Family | Periods plant/controller/maneuver/KPI | Latencies sensing/command/maneuver/state | Reference | Max position error (m) | Max speed error (m/s) | Max command error (m/s²) | Minimum gap (m) | Baseline Δgap (m) | Envelope |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        row = result["row"]
        errors = result["comparison"]["max_abs_error"]
        exceeded = result["exceeded_envelope"]
        envelope = "EXCEEDED: " + ", ".join(exceeded) if exceeded else "within"
        baseline = result.get("sensitivity_comparison")
        baseline_delta = (
            "-" if baseline is None
            else f"{baseline['minimum_gap_delta_m']:.6g}"
        )
        lines.append(
            f"| {row['name']} | {row['family']} | {_periods(row)} | {_latencies(row)} | "
            f"{row['reference']} | "
            f"{max(errors.get('gap_m', 0.0), errors.get('lead_position_m', 0.0), errors.get('ego_position_m', 0.0)):.6g} | "
            f"{max(errors.get('ego_speed_mps', 0.0), errors.get('lead_speed_mps', 0.0), errors.get('relative_speed_mps', 0.0)):.6g} | "
            f"{errors.get('accel_mps2', 0.0):.6g} | "
            f"{result['comparison']['minimum_gap_m']:.6g} | {baseline_delta} | {envelope} |"
        )
    return "\n".join(lines) + "\n"


def report_markdown(
    configuration: dict,
    results: list[dict],
    configuration_sha256: str,
    plots: list[str] | None = None,
) -> str:
    plots = [] if plots is None else plots
    plot_lines = "\n".join(f"- `{plot}`" for plot in plots)
    return "\n".join([
        "# Closed-loop communication-period sensitivity",
        "",
        "The table reports observed sensitivity; it does not assert monotonic "
        "convergence for a sampled nonlinear controller.",
        "",
        f"Configuration SHA-256: `{configuration_sha256}`",
        "",
        f"Observation policy: exact endpoint times on the "
        f"{configuration['observation_grid_ns']} ns grid; no interpolation "
        "across discontinuities.",
        "",
        "The constant-acceleration oracle is a plant sanity check. The "
        "piecewise-constant Maneuver profile is the nonconstant plant-only "
        "oracle that makes period sensitivity observable. Closed-loop plant "
        "refinement keeps controller period and modeled Channel Latencies fixed.",
        "",
        markdown_table(results).rstrip(),
        "",
        "## Plots",
        "",
        plot_lines,
        "",
    ])
