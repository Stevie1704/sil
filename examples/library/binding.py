"""The per-library binding: the one file an adopter replaces for their API.

`adapter.py` speaks the Step protocol and knows nothing about this library.
This file knows the library and nothing about the Step protocol. It declares
every symbol the adapter calls, with its exact C argument and result types,
and maps the library's lifecycle onto five calls:

- `Binding(library)` resolves every symbol, so a missing one fails before
  anything runs;
- `init(period_s, parameters)` starts one lifecycle with the Manifest's
  period and parameters;
- `step(inputs)` runs one cycle with the current input values;
- `output()` reads the latest result as the output Schema's field dict;
- `terminate()` ends the lifecycle.

`PARAMETERS`, `INPUTS` and `OUTPUTS` name what the adapter checks against
the command line and the init line's Schemas. Every failure is a
`BindingError` with the library's own error code spelled out; the adapter
decides whether it is a Manifest error or a Run failure.

To bind another library, keep these names and replace the bodies. Nothing
here is discovered: the symbols, types and meanings come from the library's
header, which is the only source that states them.
"""

from __future__ import annotations

import ctypes


class BindingError(Exception):
    """The library refused a call, or does not export one."""


class SpeedFilterConfig(ctypes.Structure):
    """`speed_filter_config` from speed_filter.h, field for field."""

    _fields_ = [
        ("period_s", ctypes.c_double),
        ("time_constant_s", ctypes.c_double),
        ("initial_speed_mps", ctypes.c_double),
    ]


class SpeedFilterResult(ctypes.Structure):
    """`speed_filter_result` from speed_filter.h, field for field."""

    _fields_ = [
        ("filtered_speed_mps", ctypes.c_double),
        ("cycles", ctypes.c_uint32),
    ]


# The negative codes of speed_filter.h, as the diagnostic says them.
ERRORS = {
    -1: "period_s must be finite and greater than 0",
    -2: "time_constant_s must be finite and at least 0",
    -3: "initial_speed_mps must be finite",
    -4: "already initialized; terminate first",
    -5: "not initialized",
    -6: "input speed_mps must be finite",
}


def _explain(call: str, status: int) -> str:
    meaning = ERRORS.get(status, "unknown error code")
    return f"{call} returned {status}: {meaning}"


class Binding:
    """speed_filter's C API, bound for the adapter."""

    PARAMETERS = ("time_constant_s", "initial_speed_mps")
    INPUTS = ("speed_mps",)
    OUTPUTS = ("filtered_speed_mps", "cycles")

    def __init__(self, library: ctypes.CDLL):
        self._init = _symbol(library, "speed_filter_init")
        self._init.argtypes = [ctypes.POINTER(SpeedFilterConfig)]
        self._init.restype = ctypes.c_int
        self._step = _symbol(library, "speed_filter_step")
        self._step.argtypes = [ctypes.c_double]
        self._step.restype = ctypes.c_int
        self._output = _symbol(library, "speed_filter_output")
        self._output.argtypes = [ctypes.POINTER(SpeedFilterResult)]
        self._output.restype = None
        self._terminate = _symbol(library, "speed_filter_terminate")
        self._terminate.argtypes = []
        self._terminate.restype = None

    def init(self, period_s: float, parameters: dict[str, float]) -> None:
        config = SpeedFilterConfig(period_s=period_s, **parameters)
        status = self._init(ctypes.byref(config))
        if status != 0:
            raise BindingError(_explain("speed_filter_init", status))

    def step(self, inputs: dict[str, float]) -> None:
        status = self._step(inputs["speed_mps"])
        if status != 0:
            raise BindingError(_explain("speed_filter_step", status))

    def output(self) -> dict:
        result = SpeedFilterResult()
        self._output(ctypes.byref(result))
        return {
            "filtered_speed_mps": result.filtered_speed_mps,
            "cycles": result.cycles,
        }

    def terminate(self) -> None:
        self._terminate()


def _symbol(library: ctypes.CDLL, name: str):
    try:
        return getattr(library, name)
    except AttributeError as error:
        raise BindingError(
            f"library {library._name!r} does not export {name!r}, "
            "which this binding calls"
        ) from error
