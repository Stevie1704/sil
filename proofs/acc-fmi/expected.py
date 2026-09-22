"""Independent oracle: stated controller values and analytic initial-value motion.

This module never imports the production model or the exporter.
"""
import math
from cases import ABS_TOL, REL_TOL

CONTROLLER = {
    (60.0, 0.0, 25.0): 1.5,
    (10.0, -5.0, 25.0): -3.0,
    (43.5, 0.0, 25.0): 0.35,
    (42.5, 0.0, 25.0): 0.0,
    (42.5, 1.0, 25.0): 1.2,
    (42.5, -1.0, 25.0): -1.2,
    (100.0, 0.0, 25.0): 1.5,
    (0.0, 0.0, 25.0): -3.0,
}


def expected(model, inputs, time):
    if model == "AccController":
        return [CONTROLLER[tuple(inputs)]]
    acceleration, = inputs
    return [25 * time + acceleration * time**2 / 2,
            25 + acceleration * time, 60 + 25 * time, 25,
            60 - acceleration * time**2 / 2, -acceleration * time]


def check(actual, wanted, context):
    if len(actual) != len(wanted):
        raise AssertionError(f"{context}: wrong field count")
    for got, want in zip(actual, wanted):
        if not math.isfinite(got) or not math.isfinite(want) or not math.isclose(
                got, want, rel_tol=REL_TOL, abs_tol=ABS_TOL):
            raise AssertionError(f"{context}: {got} != {want}")
