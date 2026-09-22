"""Comparator rejects coupling mistakes, missing final outputs and nonfinite data."""
import copy

import pytest
from closed_loop import FIELDS, STEP_NS, STEPS, compare


def data():
    rows = {n * STEP_NS: {ch: [0.0] * len(names) for ch, names in FIELDS.items()}
            for n in range(STEPS)}
    reference = [dict(slot_ns=t, communication_ns=t + STEP_NS, **copy.deepcopy(row))
                 for t, row in rows.items()]
    return rows, reference


def test_identical():
    compare(*data())


@pytest.mark.parametrize("fault", ["final", "timestamp", "nan", "command", "extra"])
def test_rejects(fault):
    rows, reference = data()
    if fault == "final":
        rows.pop((STEPS - 1) * STEP_NS)
    elif fault == "timestamp":
        reference[2]["communication_ns"] += STEP_NS
    elif fault == "nan":
        rows[0]["sensing"][0] = float("nan")
    elif fault == "command":
        rows[STEP_NS]["command"][0] = 0.1
    else:
        rows[0]["unknown"] = [0]
    with pytest.raises(RuntimeError):
        compare(rows, reference)
