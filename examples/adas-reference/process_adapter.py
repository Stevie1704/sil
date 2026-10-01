"""The ADAS reference library as a process-isolated C-library adapter.

The Native participant (`sil_adapter.c`) runs the application inside
`sil-run`. This Process participant loads the same library build with
`ctypes` and calls the application's own C API (`adas_reference.h`) in a
child process of its own: a crash or a hang of the library takes this
process, not the runner, and `--participant-timeout-ms` bounds a hang.

It does what `sil_adapter.c` does, and nothing more:

- **Configuration.** It takes the Native participant's config object as one
  JSON argument, with the same keys, the same checks and the same
  diagnostics. A rejected configuration is a Manifest error (exit 2).
- **Activation.** Each Step passes every delivered radar list, then every
  camera list, then every ego motion, each in Publish order, to the
  application, advances it over [t, t + 10 ms] and publishes one
  `adas.Command` for Sample time t + 10 ms.
- **Lists.** It checks the count against the capacity before it reads an
  element and requires every inactive element to be zero. It never
  truncates. The application checks everything else.
- **stdout.** The Step protocol owns stdout. Before the library loads, the
  protocol moves to a private descriptor and descriptor 1 points at stderr.

Only the execution form differs from the Native participant: the Command
Messages of the two forms are byte-identical (tests/test_adas_process_adapter.py).

    python3 process_adapter.py adas_reference.so '{"profile": ...}'
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import sys

from sil.participant import (
    ManifestError,
    ParticipantFailure,
    StepParticipant,
    run,
)

PROFILE = "sil.adas-reference.radar-camera"
PROFILE_VERSION = 3
MAX_OBJECTS = 8
OK = 0

# The config keys and their kinds, in the order sil_adapter.c reads them.
CONFIG_KEYS = {"profile": "string", "profile_version": "unsigned",
               "radar": "string", "camera": "string", "ego": "string",
               "command": "string", "period_ns": "unsigned",
               "hazard_acceleration_mps2": "number",
               "max_change_mps2": "number"}
# The object arrays of an adas.ObjectList, in Schema order.
OBJECT_FIELDS = ("object_id", "x_m", "y_m", "relative_vx_mps", "confidence")


# --- adas_reference.h, field for field --------------------------------------


class Config(ctypes.Structure):
    _fields_ = [("period_ns", ctypes.c_uint64),
                ("hazard_acceleration_mps2", ctypes.c_double),
                ("max_change_mps2", ctypes.c_double)]


class Object(ctypes.Structure):
    _fields_ = [("id", ctypes.c_int32), ("x_m", ctypes.c_float),
                ("y_m", ctypes.c_float), ("relative_vx_mps", ctypes.c_float),
                ("confidence", ctypes.c_float)]


class ObjectList(ctypes.Structure):
    _fields_ = [("sample_time_ns", ctypes.c_uint64),
                ("sensor_id", ctypes.c_uint32), ("frame_id", ctypes.c_uint32),
                ("sequence", ctypes.c_uint32), ("count", ctypes.c_uint32),
                ("validity", ctypes.c_uint8),
                ("objects", Object * MAX_OBJECTS)]


class Ego(ctypes.Structure):
    _fields_ = [("sample_time_ns", ctypes.c_uint64),
                ("sequence", ctypes.c_uint32), ("validity", ctypes.c_uint8),
                ("speed_mps", ctypes.c_float)]


class Output(ctypes.Structure):
    _fields_ = [("sample_time_ns", ctypes.c_uint64),
                ("sequence", ctypes.c_uint32), ("mode", ctypes.c_uint32),
                ("selected_object_id", ctypes.c_int32),
                ("target_acceleration_mps2", ctypes.c_float),
                ("acceleration_mps2", ctypes.c_float),
                ("radar_age_ns", ctypes.c_int64),
                ("camera_age_ns", ctypes.c_int64),
                ("ego_age_ns", ctypes.c_int64),
                ("ignored_observations", ctypes.c_uint32)]


class Fault(ctypes.Structure):
    _fields_ = [("message", ctypes.c_char * 160)]


class Held(ctypes.Structure):
    _fields_ = [("present", ctypes.c_int), ("sequence", ctypes.c_uint32),
                ("origin_ns", ctypes.c_uint64), ("validity", ctypes.c_uint8)]


class Instance(ctypes.Structure):
    """Caller-owned; its members are private to the application."""

    _fields_ = [("config", Config), ("acceleration_mps2", ctypes.c_double),
                ("activations", ctypes.c_uint32),
                ("ignored_observations", ctypes.c_uint32),
                ("radar_held", Held), ("camera_held", Held),
                ("ego_held", Held), ("radar", ObjectList),
                ("camera", ObjectList), ("ready", ctypes.c_int)]


# Every symbol this adapter calls: its argument types, result int.
SIGNATURES = {
    "adas_ref_init": (ctypes.POINTER(Instance), ctypes.POINTER(Config),
                      ctypes.POINTER(Fault)),
    "adas_ref_receive_radar": (ctypes.POINTER(Instance), ctypes.c_uint64,
                               ctypes.POINTER(ObjectList),
                               ctypes.POINTER(Fault)),
    "adas_ref_receive_camera": (ctypes.POINTER(Instance), ctypes.c_uint64,
                                ctypes.POINTER(ObjectList),
                                ctypes.POINTER(Fault)),
    "adas_ref_receive_ego": (ctypes.POINTER(Instance), ctypes.c_uint64,
                             ctypes.POINTER(Ego), ctypes.POINTER(Fault)),
    "adas_ref_advance": (ctypes.POINTER(Instance), ctypes.c_uint64,
                         ctypes.POINTER(Output), ctypes.POINTER(Fault)),
}


def bind(path: str) -> ctypes.CDLL:
    """Loads the library and declares every symbol this adapter calls."""
    try:
        library = ctypes.CDLL(path)
    except OSError as error:
        raise ManifestError(f"cannot load library '{path}': {error}") from error
    for name, argtypes in SIGNATURES.items():
        try:
            function = getattr(library, name)
        except AttributeError as error:
            raise ManifestError(
                f"library '{path}' does not export '{name}'") from error
        function.argtypes = argtypes
        function.restype = ctypes.c_int
    return library


# --- configuration ------------------------------------------------------------


def read_config(text: str) -> dict:
    """The Native config object, checked as sil_adapter.c checks it."""
    try:
        config = json.loads(text)
    except json.JSONDecodeError:
        raise ManifestError("config is not a JSON object") from None
    if not isinstance(config, dict):
        raise ManifestError("config is not a JSON object")
    for key, kind in CONFIG_KEYS.items():
        if key not in config:
            raise ManifestError(f"config is missing key '{key}'")
        value = config[key]
        if (kind == "string") != isinstance(value, str):
            raise ManifestError(
                f"config key '{key}' must be a "
                f"{'string' if kind == 'string' else 'number'}")
        if kind == "unsigned" and (
                isinstance(value, bool) or not isinstance(value, int)
                or not 0 <= value < 2**64):
            raise ManifestError(
                f"config key '{key}' is not an unsigned 64-bit integer: "
                f"{value}")
    for key in config:
        if key not in CONFIG_KEYS:
            raise ManifestError(f"config has unknown key '{key}'")
    if config["profile"] != PROFILE or \
            config["profile_version"] != PROFILE_VERSION:
        raise ManifestError(
            f"config names profile '{config['profile']}' version "
            f"{config['profile_version']}; this library implements "
            f"'{PROFILE}' version {PROFILE_VERSION}")
    return config


# --- the Participant ------------------------------------------------------------


class Controller(StepParticipant):
    """One application instance behind one Process participant."""

    def __init__(self, library_path: str, config: dict):
        self._library_path = library_path
        self._config = config
        self._roles = {config[role]: role for role in ("radar", "camera", "ego")}
        self._command = config["command"]
        self._instance = Instance()
        self._fault = Fault()
        self._output = Output()
        self._list = ObjectList()
        self._ego = Ego()

    def on_init(self, init: dict) -> None:
        self._library = bind(self._library_path)
        config = Config(self._config["period_ns"],
                        float(self._config["hazard_acceleration_mps2"]),
                        float(self._config["max_change_mps2"]))
        if self._library.adas_ref_init(self._instance, config,
                                       self._fault) != OK:
            raise ManifestError(self._reason())
        self._receive = {
            "radar": self._library.adas_ref_receive_radar,
            "camera": self._library.adas_ref_receive_camera,
        }

    def on_step(self, t: int, dt: int, inputs: list):
        by_role = {"radar": [], "camera": [], "ego": []}
        for message in inputs:
            by_role[self._roles[message.channel]].append(message.data)
        for sensor in ("radar", "camera"):
            for data in by_role[sensor]:
                self._to_list(t, sensor, data)
                self._call(t, self._receive[sensor], self._list)
        for data in by_role["ego"]:
            self._ego.sample_time_ns = data["sample_time_ns"]
            self._ego.sequence = data["sequence"]
            self._ego.validity = data["validity"]
            self._ego.speed_mps = data["speed_mps"]
            self._call(t, self._library.adas_ref_receive_ego, self._ego)
        self._call(t, self._library.adas_ref_advance, self._output)
        out = self._output
        return [(self._command, {
            "sample_time_ns": out.sample_time_ns, "sequence": out.sequence,
            "mode": out.mode, "selected_object_id": out.selected_object_id,
            "target_acceleration_mps2": out.target_acceleration_mps2,
            "acceleration_mps2": out.acceleration_mps2,
            "radar_age_ns": out.radar_age_ns,
            "camera_age_ns": out.camera_age_ns,
            "ego_age_ns": out.ego_age_ns,
            "ignored_observations": out.ignored_observations})]

    def _to_list(self, t: int, sensor: str, data: dict) -> None:
        count = data["count"]
        if count > MAX_OBJECTS:
            raise ParticipantFailure(
                f"t={t} ns: {sensor}.count {count} exceeds the capacity "
                f"{MAX_OBJECTS}")
        for field in OBJECT_FIELDS:
            for i in range(count, MAX_OBJECTS):
                if data[field][i] != 0 or _negative_zero(data[field][i]):
                    raise ParticipantFailure(
                        f"t={t} ns: {sensor}.{field}[{i}] is inactive (count "
                        f"{count}) but not zero; inactive elements must be "
                        "zero")
        ctypes.memset(ctypes.byref(self._list), 0, ctypes.sizeof(self._list))
        self._list.sample_time_ns = data["sample_time_ns"]
        self._list.sensor_id = data["sensor_id"]
        self._list.frame_id = data["frame_id"]
        self._list.sequence = data["sequence"]
        self._list.count = count
        self._list.validity = data["validity"]
        for i in range(count):
            self._list.objects[i] = Object(
                *(data[field][i] for field in OBJECT_FIELDS))

    def _call(self, t: int, function, argument) -> None:
        if function(self._instance, t, argument, self._fault) != OK:
            raise ParticipantFailure(f"t={t} ns: {self._reason()}")

    def _reason(self) -> str:
        return self._fault.message.decode(errors="replace")


def _negative_zero(value) -> bool:
    """-0.0 is not all-bits zero; sil_adapter.c compares bytes."""
    return isinstance(value, float) and math.copysign(1.0, value) < 0


def _reserve_protocol_stdout() -> None:
    """Give the Step protocol a descriptor the library cannot write to."""
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(protocol_fd, "w")


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit("usage: process_adapter.py LIBRARY CONFIG_JSON")
    library_path, text = argv
    _reserve_protocol_stdout()
    try:
        config = read_config(text)
    except ManifestError as error:
        # The init line has not arrived yet; answer it with the refusal.
        run(_Refusal(error))
        return
    run(Controller(library_path, config))


class _Refusal(StepParticipant):
    """Answers the init line with a configuration the adapter refused."""

    def __init__(self, error: ManifestError):
        self._error = error

    def on_init(self, init: dict) -> None:
        raise self._error


if __name__ == "__main__":
    main(sys.argv[1:])
