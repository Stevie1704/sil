"""Inspect a JRC OpenACC recording and convert one window into ACC inputs.

The recording is measured data: RT-Range differential GNSS, downsampled by
the publisher to 10 Hz. Nothing here generates or smooths a sample. Each
conversion is a declared policy:

- time: the recording's own `Time` column, rebased to the window start and
  converted to integer nanoseconds on its 100 ms grid;
- gap: `IVS<lead>`, the publisher's bumper-to-bumper spacing between vehicle
  `lead` and the vehicle behind it. The ENU antenna separation is larger and
  is never used as a gap;
- relative speed: lead Doppler speed minus ego Doppler speed;
- lead acceleration: the forward difference of the lead speed, held over its
  100 ms interval, without filtering. Integrated under that hold, it gives
  back every recorded lead speed exactly.
"""
from collections import Counter
from dataclasses import dataclass

PERIOD_MS = 100
METADATA_ROWS = 5


@dataclass(frozen=True)
class Window:
    start_s: float
    end_s: float
    lead: int
    ego: int


def _value(text):
    if text in ("", "NaN", "nan"):
        return None
    try:
        return float(text)
    except ValueError:
        return text


def parse(text):
    lines = text.splitlines()
    metadata = {}
    for line in lines[:METADATA_ROWS]:
        key, *values = line.split(",")
        metadata[key] = [value for value in values if value]
    columns = lines[METADATA_ROWS].split(",")
    rows = [dict(zip(columns, map(_value, line.split(","))))
            for line in lines[METADATA_ROWS + 1:] if line]
    return {"metadata": metadata, "columns": columns, "rows": rows}


def _milliseconds(seconds):
    return round(seconds * 1000)


def inspect(recording):
    rows = recording["rows"]
    times = [_milliseconds(row["Time"]) for row in rows]
    intervals = [b - a for a, b in zip(times, times[1:])]
    missing = Counter(column for row in rows for column, value in row.items() if value is None)
    return {
        "metadata": recording["metadata"],
        "columns": recording["columns"],
        "samples": len(rows),
        "first_time_s": rows[0]["Time"],
        "last_time_s": rows[-1]["Time"],
        "period_ns": {str(ms * 1_000_000): n for ms, n in sorted(Counter(intervals).items())},
        "gaps": [{"after_s": rows[i]["Time"], "interval_ns": ms * 1_000_000}
                 for i, ms in enumerate(intervals) if ms != PERIOD_MS],
        "missing": dict(missing),
    }


def window_rows(recording, window):
    if window.ego != window.lead + 1:
        raise ValueError("the published gap is defined only for adjacent vehicles")
    start, end = _milliseconds(window.start_s), _milliseconds(window.end_s)
    rows = [row for row in recording["rows"] if start <= _milliseconds(row["Time"]) <= end]
    if not rows or _milliseconds(rows[0]["Time"]) != start or _milliseconds(rows[-1]["Time"]) != end:
        raise ValueError(f"window [{window.start_s}, {window.end_s}] s is not inside the recording")
    times = [_milliseconds(row["Time"]) for row in rows]
    if any(b - a != PERIOD_MS for a, b in zip(times, times[1:])):
        raise ValueError(f"window [{window.start_s}, {window.end_s}] s leaves the {PERIOD_MS} ms grid")
    used = ("Time", f"Speed{window.lead}", f"Speed{window.ego}", f"IVS{window.lead}")
    if any(row.get(column) is None for row in rows for column in used):
        raise ValueError(f"window [{window.start_s}, {window.end_s}] s has a missing value")
    return rows


def controller_inputs(recording, window):
    start = _milliseconds(window.start_s)
    lead, ego = f"Speed{window.lead}", f"Speed{window.ego}"
    return [{
        "t_ns": (_milliseconds(row["Time"]) - start) * 1_000_000,
        "gap_m": row[f"IVS{window.lead}"],
        "relative_speed_mps": row[lead] - row[ego],
        "ego_speed_mps": row[ego],
    } for row in window_rows(recording, window)]


def lead_acceleration(recording, window):
    speeds = [row[f"Speed{window.lead}"] for row in window_rows(recording, window)]
    period_s = PERIOD_MS / 1000
    return [(b - a) / period_s for a, b in zip(speeds, speeds[1:])]

