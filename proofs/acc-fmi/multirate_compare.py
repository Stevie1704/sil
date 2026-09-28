"""Sample-time and common-grid judgement for the predeclared multi-rate rows."""
from __future__ import annotations

import math
import struct

from sil.recording import read_records
from multirate_contract import (ATOL, DURATION_NS, ENVELOPE, FIELDS, GRID_NS,
                                RTOL, common_grid, publication_slots,
                                sample_times)


class Difference(RuntimeError):
    pass


def recording(path, row):
    expected_slots = {name: set(publication_slots(row, name)) for name in FIELDS}
    actual = {name: {} for name in FIELDS}
    for channel, publication, payload in read_records(path):
        if channel not in FIELDS:
            raise Difference(f"unexpected Channel {channel} at publication {publication}")
        if publication in actual[channel]:
            raise Difference(f"duplicate {channel} at publication {publication}")
        fields = FIELDS[channel]
        if len(payload) != 8 * len(fields):
            raise Difference(f"width {channel} at publication {publication}")
        actual[channel][publication] = list(struct.unpack('<' + 'd' * len(fields), payload))
    for channel, slots in expected_slots.items():
        if set(actual[channel]) != slots:
            missing = sorted(slots - set(actual[channel]))
            extra = sorted(set(actual[channel]) - slots)
            raise Difference(f"{channel} publication coverage missing={missing[:1]} extra={extra[:1]}")
    return actual


def trajectory(independent):
    """An independent FMPy run's outputs, keyed like a Recording."""
    return {channel: {int(t): values for t, values in rows.items()}
            for channel, rows in independent['outputs'].items()}


def by_sample_time(values, row, channel):
    period = row.publication_period(channel)
    return {publication + period: fields for publication, fields in values[channel].items()}


def first_difference(actual, expected, row):
    """Earliest divergent Sample time across every Channel and field."""
    candidates = []
    for channel in FIELDS:
        actual_at = by_sample_time(actual, row, channel)
        expected_at = by_sample_time(expected, row, channel)
        times = sample_times(row, channel)
        if set(actual_at) != set(times) or set(expected_at) != set(times):
            raise Difference(f"{channel} Sample time coverage differs from declared grid")
        period = row.publication_period(channel)
        for time in times:
            if len(actual_at[time]) != len(FIELDS[channel]) or len(expected_at[time]) != len(FIELDS[channel]):
                raise Difference(f"{channel} width at Sample time {time}")
            found = False
            for field, value, target in zip(FIELDS[channel], actual_at[time], expected_at[time]):
                allowed = ATOL + RTOL * abs(target)
                if (not math.isfinite(value) or not math.isfinite(target) or
                        abs(value - target) > allowed):
                    candidates.append({"signal": f"{channel}.{field}",
                                       "sample_time_ns": time,
                                       "publication_ns": time - period,
                                       "actual": value, "expected": target,
                                       "tolerance": allowed})
                    found = True
                    break
            if found:
                break
    return min(candidates, key=lambda item: (item['sample_time_ns'], item['signal'])) if candidates else None


def compare_independent(actual, independent, row):
    difference = first_difference(actual, trajectory(independent), row)
    if difference:
        raise Difference(f"first differing {difference['signal']} at {difference['sample_time_ns']} ns: {difference}")
    return {"checked": {channel: len(sample_times(row, channel)) * len(fields)
                        for channel, fields in FIELDS.items()},
            "final_sample_time_ns": DURATION_NS, "atol": ATOL, "rtol": RTOL}


def sensitivity(actual, row, baseline, baseline_row):
    """Compare every field at the declared 20 ms grid shared by every Row."""
    maxima = {field: {"absolute_difference": 0.0, "sample_time_ns": GRID_NS}
              for field in ENVELOPE}
    for channel in FIELDS:
        current = by_sample_time(actual, row, channel)
        original = by_sample_time(baseline, baseline_row, channel)
        for time in common_grid():
            if time not in current or time not in original:
                raise Difference(f"missing {channel} at common Sample time {time}")
            for field, value, before in zip(FIELDS[channel], current[time], original[time]):
                difference = abs(value - before)
                if not math.isfinite(difference):
                    raise Difference(f"nonfinite {channel}.{field} at {time}")
                if difference > maxima[field]['absolute_difference']:
                    maxima[field] = {"absolute_difference": difference, "sample_time_ns": time}
    return {"grid_ns": GRID_NS, "sample_times": len(common_grid()),
            "maxima": maxima,
            "inside_envelope": all(maxima[f]['absolute_difference'] <= bound
                                   for f, bound in ENVELOPE.items())}
