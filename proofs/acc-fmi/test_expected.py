"""The gate must reject invalid numerical evidence, including NaN."""
import pytest
from expected import check


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0.001])
def test_rejects_nonfinite_or_wrong_output(value):
    with pytest.raises(AssertionError):
        check([value], [0.0], "deliberately invalid evidence")


def test_rejects_missing_output():
    with pytest.raises(AssertionError):
        check([], [0.0], "missing output")
