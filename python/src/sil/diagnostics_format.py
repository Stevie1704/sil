"""Values of the diagnostics reports as text with their units (#302, #304).

The terminal summaries (sil.diagnostics_run, sil.benchmark) and the HTML
report (sil.diagnostics_html) format every value with these functions, so a
value reads the same everywhere.
"""

from __future__ import annotations


def format_duration_ns(value: float) -> str:
    """Nanoseconds in the largest unit below the value, three decimals."""
    for unit, scale in (("s", 1e9), ("ms", 1e6), ("us", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:.3f} {unit}"
    return f"{value:.0f} ns"


def format_bytes(value: int) -> str:
    for unit, scale in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if value >= scale:
            return f"{value / scale:.3f} {unit}"
    return f"{value} B"


def format_factor(value: float) -> str:
    return f"{value:.2f}x"


def format_rate(value: float) -> str:
    return f"{value:.2f}/s"


def format_value(unit: str, value) -> str:
    """A value of a benchmark metric unit: `ns`, `bytes`, `factor`, `1/s`."""
    if unit == "ns":
        return format_duration_ns(value)
    if unit == "bytes":
        return format_bytes(value)
    if unit == "factor":
        return format_factor(value)
    return format_rate(value)
