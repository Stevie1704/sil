"""Inline SVG charts of the HTML report (#304).

Each chart draws values of the saved report and carries the same values as
text: a title per shape, an aria-label and a caption or table.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from sil.diagnostics_format import format_duration_ns, format_value
from sil.diagnostics_html_markup import (
    PHASE_LABELS, Html, number, table, tag, unavailable,
)
from sil.diagnostics_report import PHASES


HISTOGRAM_MAX_BINS = 40


def phase_chart(phases: dict) -> Html:
    names = [name for name in (*PHASES, "unattributed_ns")
             if phases.get(name)]
    total = sum(phases[name] for name in names)
    if total <= 0:
        return tag("p", "No phase has a measured time to draw.",
                   class_="note")
    width, x = 1000.0, 0.0
    bars, legend = [], []
    for index, name in enumerate(names):
        share = phases[name] / total * width
        label = f"{PHASE_LABELS[name]}: {format_duration_ns(phases[name])}"
        bars.append(tag("rect", tag("title", label), x=f"{x:.2f}", y="0",
                        width=f"{max(share, 0.5):.2f}", height="40",
                        class_=f"phase phase-{index}"))
        legend.append(tag("li", tag("span", "", class_=f"swatch phase-{index}",
                                    aria_hidden="true"), label))
        x += share
    svg = tag("svg", *bars, viewBox="0 0 1000 40", preserveAspectRatio="none",
              class_="phase-bar", role="img",
              aria_label="Lifecycle phases as shares of their sum: "
                         + "; ".join(f"{PHASE_LABELS[n]} "
                                     f"{format_duration_ns(phases[n])}"
                                     for n in names))
    return tag("figure", svg, tag("ul", *legend, class_="legend"),
               tag("figcaption", "Bar widths are the phase times in the "
                                 "table below; zero and unavailable phases "
                                 "have no bar."),
               class_="chart")


def _bins(samples: Sequence[int]) -> tuple[list[tuple[float, float, int]], bool]:
    """Histogram bins of the retained samples; log-spaced for a wide range."""
    low, high = samples[0], samples[-1]
    if low == high:
        return [(low, high, len(samples))], False
    count = min(HISTOGRAM_MAX_BINS, max(1, math.ceil(math.sqrt(len(samples)))))
    logarithmic = low > 0 and high / low >= 50
    if logarithmic:
        a, b = math.log(low), math.log(high)
        edges = [math.exp(a + (b - a) * i / count) for i in range(count + 1)]
    else:
        edges = [low + (high - low) * i / count for i in range(count + 1)]
    counts = [0] * count
    for value in samples:
        position = 0
        while position < count - 1 and value >= edges[position + 1]:
            position += 1
        counts[position] += 1
    return ([(edges[i], edges[i + 1], counts[i]) for i in range(count)],
            logarithmic)


def histogram(id_: str, title: str, timing: dict, anchor: str) -> Html:
    quantiles = timing["quantiles"]
    samples = timing["samples_ns"]
    retained, total = quantiles["retained_count"], quantiles["sample_count"]
    if quantiles["approximate"]:
        sampled = (f"Sampled data: a uniform reservoir of {retained} of "
                   f"{total} completed activations; percentiles are "
                   "approximate.")
    else:
        sampled = (f"Sampled data: all {retained} of {total} completed "
                   "activations retained; percentiles are exact.")
    heading = tag("h4", tag("a", title, href=anchor))
    if not samples:
        reason = timing.get("unavailable", {}).get("mean_ns",
                                                    "no_completed_activations")
        return tag("figure", heading,
                   tag("p", "No retained sample to draw: ",
                       unavailable(reason), class_="empty-chart"),
                   class_="chart histogram", id=id_)
    bins, logarithmic = _bins(samples)
    tallest = max(count for _a, _b, count in bins)
    width, height, left, bottom = 600.0, 180.0, 44.0, 30.0
    plot_w, plot_h = width - left - 8, height - bottom - 10
    bar_w = plot_w / len(bins)
    shapes: list[Any] = [
        tag("line", x1=f"{left}", y1=f"{10 + plot_h}", x2=f"{width - 8}",
            y2=f"{10 + plot_h}", class_="axis"),
        tag("text", str(tallest), x=f"{left - 6}", y="18", class_="tick",
            text_anchor="end"),
        tag("text", "0", x=f"{left - 6}", y=f"{10 + plot_h}", class_="tick",
            text_anchor="end"),
    ]
    for index, (start, end, count) in enumerate(bins):
        bar_h = plot_h * count / tallest
        shapes.append(tag(
            "rect",
            tag("title", f"{format_duration_ns(start)} to "
                         f"{format_duration_ns(end)}: {count} samples"),
            x=f"{left + index * bar_w + 0.5:.2f}",
            y=f"{10 + plot_h - bar_h:.2f}",
            width=f"{max(bar_w - 1, 0.5):.2f}", height=f"{bar_h:.2f}",
            class_="bar"))

    def x_of(value: float) -> float:
        low, high = bins[0][0], bins[-1][1]
        if high == low:
            return left + plot_w / 2
        if logarithmic:
            return left + plot_w * (math.log(value) - math.log(low)) / (
                math.log(high) - math.log(low))
        return left + plot_w * (value - low) / (high - low)

    marks = []
    for label, key in (("p50", "p50_ns"), ("p95", "p95_ns"), ("p99", "p99_ns")):
        value = timing.get(key)
        if value is None:
            marks.append(f"{label} unavailable "
                         f"({timing['unavailable'].get(key, 'not reported')})")
            continue
        x = min(max(x_of(value), left), width - 8)
        shapes.append(tag("line", x1=f"{x:.2f}", y1="10", x2=f"{x:.2f}",
                          y2=f"{10 + plot_h}", class_="marker"))
        shapes.append(tag("text", label, x=f"{x + 3:.2f}", y="20",
                          class_="marker-label"))
        marks.append(f"{label} {format_duration_ns(value)}")
    shapes.append(tag("text", format_duration_ns(bins[0][0]), x=f"{left}",
                      y=f"{height - 8}", class_="tick"))
    shapes.append(tag("text", format_duration_ns(bins[-1][1]),
                      x=f"{width - 8}", y=f"{height - 8}", class_="tick",
                      text_anchor="end"))
    scale = "logarithmic time axis" if logarithmic else "linear time axis"
    description = (f"Histogram of {retained} retained samples of {title}, "
                   f"{scale}, from {format_duration_ns(bins[0][0])} to "
                   f"{format_duration_ns(bins[-1][1])}; " + "; ".join(marks))
    svg = tag("svg", *shapes, viewBox=f"0 0 {width:.0f} {height:.0f}",
              class_="histogram-svg", role="img", aria_label=description)
    bin_rows = [[f"{format_duration_ns(start)} to {format_duration_ns(end)}",
                 (number(count, "count"), str(count))]
                for start, end, count in bins]
    return tag(
        "figure", heading, svg,
        tag("figcaption", sampled, " ", tag("span", scale, class_="muted"),
            " · ", "; ".join(marks)),
        tag("details", tag("summary", "Bin counts as a table"),
            table(f"{id_}-bins", f"Samples per bin of {title}",
                  [("Wall time range", False), ("Samples", True)], bin_rows,
                  filterable=False)),
        class_="chart histogram", id=id_)


def bar_chart(id_: str, title: str, unit: str,
               bars: Sequence[tuple[str, Any, str, str]],
               spread: dict | None) -> Html:
    """Per-Run bars: (label, value or None, role, tone)."""
    values = [value for _l, value, _r, _t in bars if value is not None]
    if not values:
        return tag("figure", tag("h4", title),
                   tag("p", "No Run report has a value to draw.",
                       class_="empty-chart"), class_="chart", id=id_)

    def fmt(value: Any) -> str:
        return format_value(unit, value)

    top = max(values + ([spread["max"]] if spread and spread["max"] is not None
                        else []))
    top = top if top > 0 else 1
    width, left, bottom, height = 600.0, 8.0, 46.0, 220.0
    plot_h = height - bottom - 24
    slot = (width - left * 2) / len(bars)
    shapes: list[Any] = []
    base = 24 + plot_h
    if spread and spread["min"] is not None:
        y_max = base - plot_h * spread["max"] / top
        y_min = base - plot_h * spread["min"] / top
        y_med = base - plot_h * spread["median"] / top
        shapes += [
            tag("rect", tag("title", f"Aggregate spread: min "
                                     f"{fmt(spread['min'])}, max "
                                     f"{fmt(spread['max'])}"),
                x=f"{left}", y=f"{y_max:.2f}", width=f"{width - 2 * left}",
                height=f"{max(y_min - y_max, 1):.2f}", class_="spread"),
            tag("line", x1=f"{left}", x2=f"{width - left}", y1=f"{y_med:.2f}",
                y2=f"{y_med:.2f}", class_="median"),
            tag("text", f"median {fmt(spread['median'])}", x=f"{width - left}",
                y=f"{y_med - 4:.2f}", text_anchor="end", class_="median-label"),
        ]
    shapes.append(tag("line", x1=f"{left}", x2=f"{width - left}",
                      y1=f"{base}", y2=f"{base}", class_="axis"))
    described = []
    for index, (label, value, role, tone) in enumerate(bars):
        x = left + index * slot
        marker = {"fail": "✕ ", "warn": "! "}.get(tone, "")
        role_text = "warm-up" if role == "warmup" else ""
        if value is None:
            shapes.append(tag("text", "no value", x=f"{x + slot / 2:.2f}",
                              y=f"{base - 6}", text_anchor="middle",
                              class_="tick"))
            described.append(f"{label} no value")
        else:
            bar_h = plot_h * value / top
            shapes.append(tag(
                "rect", tag("title", f"{label}: {fmt(value)}"),
                x=f"{x + slot * 0.15:.2f}", y=f"{base - bar_h:.2f}",
                width=f"{slot * 0.7:.2f}", height=f"{bar_h:.2f}",
                class_=f"bar role-{role} tone-{tone}"))
            described.append(f"{label} {fmt(value)}"
                             + (" warm-up" if role == "warmup" else "")
                             + (" failed" if tone == "fail" else ""))
        every = max(1, math.ceil(70 / slot))
        short = label if slot >= 70 else label.rsplit("-", 1)[-1]
        if index % every == 0 or tone != "ok":
            shapes.append(tag("text", marker + short,
                              x=f"{x + slot / 2:.2f}", y=f"{base + 16}",
                              text_anchor="middle", class_="tick"))
        if role_text and slot >= 70:
            shapes.append(tag("text", role_text, x=f"{x + slot / 2:.2f}",
                              y=f"{base + 30}", text_anchor="middle",
                              class_="tick muted-text"))
    svg = tag("svg", *shapes, viewBox=f"0 0 {width:.0f} {height:.0f}",
              class_="bars-svg", role="img",
              aria_label=f"{title} per Run: " + "; ".join(described))
    return tag(
        "figure", tag("h4", title), svg,
        tag("figcaption",
            "Dashed outline bars are warm-ups; \u2715 marks a failed Run. ",
            "The band is the min-max spread and the line the median of the "
            "successful measured Runs, from the aggregates."
            if spread and spread["min"] is not None else
            "No aggregate spread: " + (
                ", ".join(sorted(set(spread["unavailable"].values())))
                if spread else "no aggregate metric for this scope") + "."),
        class_="chart", id=id_)
