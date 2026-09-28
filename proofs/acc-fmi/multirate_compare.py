"""Endpoint and common-grid judgement for the predeclared multi-rate rows."""
from __future__ import annotations

import math
import struct

from sil.recording import read_records
from multirate_contract import (ATOL, DURATION_NS, ENVELOPE, FIELDS, GRID_NS,
                                RTOL, common_grid, observation_times,
                                publication_slots)


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


def endpoints(values, row, channel):
    period = row.periods()['controller' if channel == 'command' else 'plant']
    return {publication + period: fields for publication, fields in values[channel].items()}


def first_difference(actual, expected, row, *, channels=FIELDS):
    """Earliest divergent observation across every checked Channel and field."""
    candidates = []
    for channel in channels:
        actual_at = endpoints(actual, row, channel)
        expected_at = endpoints(expected, row, channel)
        times = observation_times(row, channel)
        if set(actual_at) != set(times) or set(expected_at) != set(times):
            raise Difference(f"{channel} endpoint coverage differs from declared grid")
        period = row.periods()['controller' if channel == 'command' else 'plant']
        for time in times:
            if len(actual_at[time]) != len(FIELDS[channel]) or len(expected_at[time]) != len(FIELDS[channel]):
                raise Difference(f"{channel} width at observation {time}")
            found = False
            for field, value, target in zip(FIELDS[channel], actual_at[time], expected_at[time]):
                allowed = ATOL + RTOL * abs(target)
                if (not math.isfinite(value) or not math.isfinite(target) or
                        abs(value - target) > allowed):
                    candidates.append({"signal": f"{channel}.{field}",
                                       "observation_ns": time,
                                       "publication_ns": time - period,
                                       "actual": value, "expected": target,
                                       "tolerance": allowed})
                    found = True
                    break
            if found:
                break
    return min(candidates, key=lambda item: (item['observation_ns'], item['signal'])) if candidates else None


def compare_reference(actual, reference, row):
    expected = {channel: {int(t): values for t, values in rows.items()}
                for channel, rows in reference['outputs'].items()}
    difference = first_difference(actual, expected, row)
    if difference:
        raise Difference(f"first differing {difference['signal']} at {difference['observation_ns']} ns: {difference}")
    return {"checked": {channel: len(observation_times(row, channel)) * len(fields)
                        for channel, fields in FIELDS.items()},
            "final_observation_ns": DURATION_NS, "atol": ATOL, "rtol": RTOL}


def sensitivity(actual, row, baseline, baseline_row):
    """Compare at the declared 20 ms endpoints shared by every tested row."""
    maxima = {field: {"absolute_difference": 0.0, "observation_ns": GRID_NS}
              for field in ENVELOPE}
    for channel in ('sensing', 'command'):
        current = endpoints(actual, row, channel)
        original = endpoints(baseline, baseline_row, channel)
        for time in common_grid():
            if time not in current or time not in original:
                raise Difference(f"missing {channel} at common observation {time}")
            for field, value, reference in zip(FIELDS[channel], current[time], original[time]):
                if field not in maxima:
                    continue
                difference = abs(value - reference)
                if not math.isfinite(difference):
                    raise Difference(f"nonfinite {channel}.{field} at {time}")
                if difference > maxima[field]['absolute_difference']:
                    maxima[field] = {"absolute_difference": difference, "observation_ns": time}
    return {"grid_ns": GRID_NS, "observations": len(common_grid()),
            "maxima": maxima,
            "inside_envelope": all(maxima[f]['absolute_difference'] <= bound
                                   for f, bound in ENVELOPE.items())}
