"""Post-hoc KPIs and variant effects of the mixed closed loop (issue #227).

Each function reads decoded Messages, `[(publication_ns, fields), ...]`, of
one Recording: the Commands on `adas.command` and the truth on `loop.truth`.
Both are published one Step before their Sample time. The thresholds and
expected modes come from loop.py, fixed before any Run.
"""

from __future__ import annotations

import math

Messages = list[tuple[int, dict]]


def coverage(commands: Messages, truth: Messages, step_ns: int,
             duration_ns: int) -> list[str]:
    """One Command and one truth Message in every Slot, the first through
    the final one: the last Command describes the Duration."""
    findings = []
    slots = list(range(0, duration_ns, step_ns))
    for name, messages in (("Commands", commands), ("truth", truth)):
        got = [t for t, _ in messages]
        if got != slots:
            findings.append(f"{name} in {len(got)} Slots, first "
                            f"{got[:1]}, last {got[-1:]}; expected "
                            f"{len(slots)} from 0 to {slots[-1]}")
    for k, (t, fields) in enumerate(commands):
        if fields["sample_time_ns"] != t + step_ns:
            findings.append(f"Slot {t}: Sample time {fields['sample_time_ns']}")
            break
        if fields["sequence"] != k + 1:
            findings.append(f"Slot {t}: sequence {fields['sequence']}")
            break
    return findings


def mode_runs(commands: Messages, step_ns: int) -> list[tuple[int, int]]:
    """The modes in order, each with the Sample time it starts at."""
    runs: list[tuple[int, int]] = []
    for t, fields in commands:
        if not runs or runs[-1][0] != fields["mode"]:
            runs.append((fields["mode"], t + step_ns))
    return runs


def mode_findings(runs: list[tuple[int, int]],
                  expected: tuple[tuple[int, int | None], ...]) -> list[str]:
    if [m for m, _ in runs] != [m for m, _ in expected]:
        return [f"modes {runs}, expected {list(expected)}"]
    return [f"mode {mode} starts at {start} ns, predicted {predicted} ns"
            for (mode, start), (_, predicted) in zip(runs, expected)
            if predicted is not None and start != predicted]


def first_violation(commands: Messages, truth: Messages, kpi: dict,
                    step_ns: int) -> dict | None:
    """The earliest Sample time at which a KPI fails, with its field."""
    checks = []
    for t, f in truth:
        for name, value, low, high in (
                ("gap_m", f["gap_m"], kpi["minimum_gap_m"], math.inf),
                ("ego_speed_mps", f["ego_speed_mps"],
                 kpi["minimum_ego_speed_mps"], math.inf)):
            checks.append((t + step_ns, name, value, low, high))
    for t, f in commands:
        checks.append((t + step_ns, "acceleration_mps2",
                       f["acceleration_mps2"], kpi["accel_min_mps2"],
                       kpi["accel_max_mps2"]))
    for sample, name, value, low, high in sorted(checks, key=lambda c: c[0]):
        if not math.isfinite(value) or not low <= value <= high:
            return {"sample_time_ns": sample, "field": name, "value": value,
                    "bounds": [low, high]}
    return None


def summary(commands: Messages, truth: Messages) -> dict:
    accels = [f["acceleration_mps2"] for _, f in commands]
    return {
        "minimum_gap_m": min(f["gap_m"] for _, f in truth),
        "minimum_ego_speed_mps": min(f["ego_speed_mps"] for _, f in truth),
        "final_ego_speed_mps": truth[-1][1]["ego_speed_mps"],
        "accel_range_mps2": [min(accels), max(accels)],
    }


def hazard_onset(commands: Messages, hazard: int, step_ns: int) -> int | None:
    return next((t + step_ns for t, f in commands if f["mode"] == hazard),
                None)


def effect(baseline: tuple[Messages, Messages],
           variant: tuple[Messages, Messages], hazard: int, step_ns: int,
           envelope: dict) -> dict:
    """How far a variant moves the behavior from its baseline, against the
    declared envelope. The first differing Command shows the variant
    changes what the controller sees at all."""
    (base_cmd, base_truth), (var_cmd, var_truth) = baseline, variant
    onsets = (hazard_onset(base_cmd, hazard, step_ns),
              hazard_onset(var_cmd, hazard, step_ns))
    gaps = (summary(base_cmd, base_truth)["minimum_gap_m"],
            summary(var_cmd, var_truth)["minimum_gap_m"])
    first = next((t + step_ns for (t, a), (_, b) in zip(base_cmd, var_cmd)
                  if a != b), None)
    findings = []
    if None in onsets:
        findings.append(f"no hazard onset: {onsets}")
    else:
        low, high = envelope["hazard_onset_shift_ns"]
        if not low <= onsets[1] - onsets[0] <= high:
            findings.append(f"hazard onset shift {onsets[1] - onsets[0]} ns "
                            f"outside [{low}, {high}]")
    if abs(gaps[1] - gaps[0]) > envelope["minimum_gap_delta_m"]:
        findings.append(f"minimum gap moves {gaps[1] - gaps[0]} m")
    if first is None:
        findings.append("the variant changes no Command")
    return {"hazard_onset_ns": list(onsets), "minimum_gap_m": list(gaps),
            "first_differing_command_ns": first, "findings": findings}
