"""Create compact, reviewable evidence for the sensitivity experiment."""
from __future__ import annotations

import csv
import html
import json
import shutil
import sys
from pathlib import Path

from proof_support import file_sha256, write_json


def _scale(value: float, low: float, high: float, start: float, size: float) -> float:
    if high <= low:
        return start + size / 2
    return start + (value - low) / (high - low) * size


def _svg_line_plot(
    path: Path,
    title: str,
    series: list[tuple[str, list[float], str]],
    labels: list[str],
    *,
    y_label: str,
    reference_line: float | None = None,
) -> None:
    width, height = 1_280, 560
    left, right, top, bottom = 86, 36, 56, 150
    plot_width, plot_height = width - left - right, height - top - bottom
    values = [value for _name, points, _colour in series for value in points]
    if reference_line is not None:
        values.append(reference_line)
    low = min(0.0, min(values, default=0.0))
    high = max(1.0, max(values, default=1.0))
    padding = (high - low) * 0.08 or 1.0
    low -= padding
    high += padding

    def point(index: int, value: float) -> tuple[float, float]:
        x = left if len(labels) == 1 else left + index * plot_width / (len(labels) - 1)
        y = top + plot_height - _scale(value, low, high, 0, plot_height)
        return x, y

    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{left}" y="30" font-family="sans-serif" font-size="20" '
        f'font-weight="bold">{html.escape(title)}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" '
        'stroke="#333"/>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" '
        f'y2="{top + plot_height}" stroke="#333"/>',
        f'<text x="18" y="{top + plot_height / 2}" transform="rotate(-90 18 '
        f'{top + plot_height / 2})" font-family="sans-serif" font-size="13">'
        f'{html.escape(y_label)}</text>',
    ]
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        value = low + (high - low) * fraction
        y = top + plot_height - plot_height * fraction
        chunks.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" '
            f'y2="{y:.2f}" stroke="#ddd"/><text x="{left - 8}" y="{y + 4:.2f}" '
            f'text-anchor="end" font-family="sans-serif" font-size="11">'
            f'{value:.4g}</text>'
        )
    for index, label in enumerate(labels):
        x, _ = point(index, 0)
        chunks.append(
            f'<text x="{x:.2f}" y="{top + plot_height + 22}" '
            'text-anchor="end" transform="rotate(-55 '
            f'{x:.2f} {top + plot_height + 22})" font-family="sans-serif" '
            f'font-size="10">{html.escape(label)}</text>'
        )
    if reference_line is not None:
        _x1, y = point(0, reference_line)
        _x2, _ = point(max(len(labels) - 1, 0), reference_line)
        chunks.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" '
            f'y2="{y:.2f}" stroke="#b33" stroke-dasharray="7 5"/>'
        )
    for name, points, colour in series:
        coords = " ".join(f"{point(i, value)[0]:.2f},{point(i, value)[1]:.2f}"
                          for i, value in enumerate(points))
        chunks.append(
            f'<polyline points="{coords}" fill="none" stroke="{colour}" '
            'stroke-width="2.5"/>'
        )
        for i, value in enumerate(points):
            x, y = point(i, value)
            chunks.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="{colour}"/>')
    legend_x = left
    for name, _points, colour in series:
        chunks.extend([
            f'<line x1="{legend_x}" y1="{height - 30}" x2="{legend_x + 24}" '
            f'y2="{height - 30}" stroke="{colour}" stroke-width="3"/>',
            f'<text x="{legend_x + 30}" y="{height - 26}" font-family="sans-serif" '
            f'font-size="12">{html.escape(name)}</text>',
        ])
        legend_x += 180
    if reference_line is not None:
        chunks.append(
            f'<line x1="{legend_x}" y1="{height - 30}" x2="{legend_x + 24}" '
            f'y2="{height - 30}" stroke="#b33" stroke-dasharray="7 5"/>',
        )
        chunks.append(
            f'<text x="{legend_x + 30}" y="{height - 26}" font-family="sans-serif" '
            'font-size="12">declared envelope</text>',
        )
    chunks.append("</svg>\n")
    path.write_text("".join(chunks))


def write_plots(out: Path, results: list[dict]) -> list[str]:
    labels = [result["row"]["name"] for result in results]
    deviations = [
        result.get("sensitivity_comparison") or result["comparison"]
        for result in results
    ]
    position = [item["max_abs_error"].get("lead_position_m", 0.0) for item in deviations]
    gap = [item["max_abs_error"].get("gap_m", 0.0) for item in deviations]
    minimum_gap = [result["comparison"]["minimum_gap_m"] for result in results]
    reference_gap = [result["comparison"]["reference_minimum_gap_m"] for result in results]

    _svg_line_plot(
        out / "sensitivity-error.svg",
        "Observed position and gap deviation by authored row",
        [("lead position error (m)", position, "#1769aa"),
         ("gap error (m)", gap, "#d17a00")],
        labels,
        y_label="absolute deviation",
        reference_line=1.0,
    )
    _svg_line_plot(
        out / "sensitivity-kpi.svg",
        "Minimum-gap KPI by authored row",
        [("measured minimum gap (m)", minimum_gap, "#238636"),
         ("reference minimum gap (m)", reference_gap, "#8250df")],
        labels,
        y_label="minimum gap (m)",
    )

    with (out / "sensitivity-plot-data.csv").open("w", newline="") as opened:
        writer = csv.writer(opened)
        writer.writerow([
            "row", "family", "position_error_m", "gap_error_m",
            "minimum_gap_m", "reference_minimum_gap_m",
        ])
        for result, position_error, gap_error in zip(results, position, gap):
            writer.writerow([
                result["row"]["name"], result["row"]["family"],
                position_error, gap_error,
                result["comparison"]["minimum_gap_m"],
                result["comparison"]["reference_minimum_gap_m"],
            ])
    return ["sensitivity-error.svg", "sensitivity-kpi.svg", "sensitivity-plot-data.csv"]


def retain(source: Path, target: Path) -> None:
    """Retain compact report inputs while leaving full raw traces in CI."""
    target.mkdir(parents=True, exist_ok=True)
    report = {
        name: json.loads((source / f"{name}.json").read_text())
        for name in ("results", "environment", "configuration")
    }
    report["sensitivity_report"] = json.loads(
        (source / "sensitivity-report.json").read_text()
    )
    report["fmus"] = {
        name: json.loads((source / f"{name}.identity.json").read_text())
        for name in ("AccController", "AccPlant")
    }
    report["artifacts_sha256"] = {
        path.name: file_sha256(path)
        for path in sorted(source.iterdir())
        if path.is_file() and path.suffix in (
            ".json", ".mcap", ".fmu", ".svg", ".csv", ".md",
        )
    }
    write_json(target / "report.json", report)
    for name in (
        "sensitivity-report.md", "configuration.json", "results.json",
        "sensitivity-error.svg", "sensitivity-kpi.svg", "sensitivity-plot-data.csv",
    ):
        shutil.copyfile(source / name, target / name)
    if (source / "image-id.txt").exists():
        shutil.copyfile(source / "image-id.txt", target / "image-id.txt")


if __name__ == "__main__":
    retain(Path(sys.argv[1]), Path(sys.argv[2]))
