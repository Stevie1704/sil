"""The port binding of gap_monitor: several Channels, several entry points.

`adapter.py --binding gap_binding.py` loads this file instead of
`binding.py`. It declares the library's ports and cyclic entry points with
the types of `ports.py`, and maps its lifecycle onto six calls:

- `Binding(library)` resolves every symbol, so a missing one fails before
  anything runs;
- `init(period_s, parameters)` starts one lifecycle with the adapter's Step
  Period and the parameters;
- `write(port, fields)` gives the library one input port's held value;
- `run(entry)` executes one entry point;
- `read(port)` reads one output port as its Schema's field dict;
- `terminate()` ends the lifecycle.

Every failure is a `BindingError` with the library's own error code spelled
out. The symbols, types and meanings come from gap_monitor.h.
"""

from __future__ import annotations

import ctypes

from ports import EntryPoint, Field, InputPort, OutputPort

MS = 1_000_000


class BindingError(Exception):
    """The library refused a call, or does not export one."""


class GapMonitorConfig(ctypes.Structure):
    _fields_ = [("period_s", ctypes.c_double),
                ("warning_gap_s", ctypes.c_double)]


class GapMonitorEgo(ctypes.Structure):
    _fields_ = [("speed_mps", ctypes.c_double)]


class GapMonitorRadar(ctypes.Structure):
    _fields_ = [("range_m", ctypes.c_double),
                ("object_id", ctypes.c_uint32)]


class GapMonitorGap(ctypes.Structure):
    _fields_ = [("time_gap_s", ctypes.c_double),
                ("object_id", ctypes.c_uint32),
                ("track_cycles", ctypes.c_uint32)]


class GapMonitorReport(ctypes.Structure):
    _fields_ = [("min_time_gap_s", ctypes.c_double),
                ("warnings", ctypes.c_uint32),
                ("report_cycles", ctypes.c_uint32)]


# The negative codes of gap_monitor.h, as the diagnostic says them.
ERRORS = {
    -1: "period_s must be finite and greater than 0",
    -2: "warning_gap_s must be finite and at least 0",
    -3: "already initialized; terminate first",
    -4: "not initialized",
    -5: "ego speed_mps must be finite and greater than 0",
    -6: "radar range_m must be finite and at least 0",
}


def _explain(call: str, status: int) -> str:
    meaning = ERRORS.get(status, "unknown error code")
    return f"{call} returned {status}: {meaning}"


def _fields(struct: type[ctypes.Structure], *types: str) -> tuple[Field, ...]:
    return tuple(Field(name, kind)
                 for (name, _), kind in zip(struct._fields_, types, strict=True))


class Binding:
    """gap_monitor's C API, bound for the adapter's port contract."""

    PARAMETERS = ("warning_gap_s",)
    INPUT_PORTS = (
        InputPort("ego", _fields(GapMonitorEgo, "f64")),
        InputPort("radar", _fields(GapMonitorRadar, "f64", "u32")),
    )
    OUTPUT_PORTS = (
        OutputPort("gap", "track", _fields(GapMonitorGap, "f64", "u32", "u32")),
        OutputPort("report", "report",
                   _fields(GapMonitorReport, "f64", "u32", "u32")),
    )
    # Declared order is execution order: when both are due, `report`
    # summarizes the `track` cycle of the same Step.
    ENTRY_POINTS = (
        EntryPoint("track", period_ns=10 * MS),
        EntryPoint("report", period_ns=30 * MS, offset_ns=10 * MS),
    )

    def __init__(self, library: ctypes.CDLL):
        self._init = _symbol(library, "gap_monitor_init",
                             [ctypes.POINTER(GapMonitorConfig)], ctypes.c_int)
        self._inputs = {
            "ego": (GapMonitorEgo, _symbol(
                library, "gap_monitor_set_ego",
                [ctypes.POINTER(GapMonitorEgo)], ctypes.c_int)),
            "radar": (GapMonitorRadar, _symbol(
                library, "gap_monitor_set_radar",
                [ctypes.POINTER(GapMonitorRadar)], ctypes.c_int)),
        }
        self._entries = {
            "track": _symbol(library, "gap_monitor_track", [], ctypes.c_int),
            "report": _symbol(library, "gap_monitor_report_cycle", [],
                              ctypes.c_int),
        }
        self._outputs = {
            "gap": (GapMonitorGap, _symbol(
                library, "gap_monitor_gap_output",
                [ctypes.POINTER(GapMonitorGap)], None)),
            "report": (GapMonitorReport, _symbol(
                library, "gap_monitor_report_output",
                [ctypes.POINTER(GapMonitorReport)], None)),
        }
        self._terminate = _symbol(library, "gap_monitor_terminate", [], None)

    def init(self, period_s: float, parameters: dict[str, float]) -> None:
        config = GapMonitorConfig(period_s=period_s, **parameters)
        status = self._init(ctypes.byref(config))
        if status != 0:
            raise BindingError(_explain("gap_monitor_init", status))

    def write(self, port: str, fields: dict) -> None:
        struct, call = self._inputs[port]
        status = call(ctypes.byref(struct(**fields)))
        if status != 0:
            raise BindingError(_explain(call.__name__, status))

    def run(self, entry: str) -> None:
        call = self._entries[entry]
        status = call()
        if status != 0:
            raise BindingError(_explain(call.__name__, status))

    def read(self, port: str) -> dict:
        struct, call = self._outputs[port]
        result = struct()
        call(ctypes.byref(result))
        return {name: getattr(result, name) for name, _ in struct._fields_}

    def terminate(self) -> None:
        self._terminate()


def _symbol(library: ctypes.CDLL, name: str, argtypes: list, restype):
    try:
        function = getattr(library, name)
    except AttributeError as error:
        raise BindingError(
            f"does not export {name!r}, which this binding calls"
        ) from error
    function.argtypes = argtypes
    function.restype = restype
    return function
