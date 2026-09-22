"""Recording and independent-trajectory checks, without an FMI dependency."""
import math
import struct

from sil.recording import read_records
from loop_contract import FIELDS, FMU_SCHEDULE, INITIAL_OUTPUTS, STEP_NS, STEPS, TOLERANCES
from proof_support import require


def recording_messages(path, schedule=FMU_SCHEDULE):
    rows = {}
    channels = dict(schedule.channels)
    for channel, t, payload in read_records(path):
        require(channel in channels, f"unknown Channel {channel}")
        name = channels[channel]
        require(t % STEP_NS == 0 and 0 <= t < STEPS * STEP_NS, f"invalid timestamp {channel}@{t}")
        row = rows.setdefault(t, {})
        require(name not in row, f"duplicate {channel}@{t}")
        row[name] = list(struct.unpack("<" + "d" * len(FIELDS[name]), payload))
    return rows


def check_values(channel, values, expected, instant):
    require(len(values) == len(expected) == len(FIELDS[channel]), f"width {channel} {instant}")
    for field, actual, wanted in zip(FIELDS[channel], values, expected):
        absolute, relative = TOLERANCES[field]
        require(math.isfinite(actual) and math.isfinite(wanted) and
                math.isclose(actual, wanted, abs_tol=absolute, rel_tol=relative),
                f"first mismatch {field} {instant}: SiL={actual}, reference={wanted}")


def validate_reference(document):
    require(document["grid"] == dict(step_ns=STEP_NS, steps=STEPS), "reference communication grid differs")
    require(set(document["initialization"]) == set(INITIAL_OUTPUTS), "initialization fields differ")
    for channel, expected in INITIAL_OUTPUTS.items():
        check_values(channel, document["initialization"][channel], expected, "initialization @0")
    reference = document["intervals"]
    require(len(reference) == STEPS, "reference communication-point count differs")
    for n, row in enumerate(reference):
        t = n * STEP_NS
        require(row["slot_ns"] == t and row["interval_end_ns"] == t + STEP_NS,
                f"reference timestamp mismatch @{t}")
    return reference


def compare(rows, document, schedule=FMU_SCHEDULE):
    reference = validate_reference(document)
    require(set(rows) == {n * STEP_NS for n in range(STEPS)}, "Recording Slot count/timestamps differ")
    for n, expected in enumerate(reference):
        t = n * STEP_NS
        names = schedule.channels_at(n)
        require(set(rows[t]) == names, f"missing/extra Channel @{t}: {set(rows[t])}")
        for channel in sorted(names):
            check_values(channel, rows[t][channel], expected[channel],
                         f"publication={t} communication={schedule.communication_ns(channel, t)}")


def first_difference(nominal, variant, channel):
    """A changed field in actual SiL Messages, using the declared tolerances."""
    require(set(nominal) == set(variant), "variant Recording Slot count/timestamps differ")
    for t in sorted(nominal):
        try:
            check_values(channel, nominal[t][channel], variant[t][channel], f"publication={t}")
        except RuntimeError as error:
            return dict(publication_ns=t, diagnostic=str(error))
    raise RuntimeError(f"SiL perturbation did not change Channel {channel}")
