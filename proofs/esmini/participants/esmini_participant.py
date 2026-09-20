"""The consumer-side adapter that drives esmini as a SiL Process participant.

It is written against two published contracts and nothing else: the Step
protocol (through the `sil.participant` endpoint installed in the SiL runner
image) and esmini's own `esminiLib` C API. It loads `libesminiLib.so` with
ctypes, maps one Step onto `SE_StepDT(dt)`, and publishes the state of the
named scenario objects on the Channels the Manifest declares.

Mapping decisions, all of them consumer-side:

- **Virtual time.** The Step at virtual time `t` publishes esmini's state at
  scenario time `t`. `t = 0` is therefore published without stepping — that is
  the state `SE_Init` leaves behind — and every later activation advances the
  scenario by exactly the Manifest's Step period first.
- **Fixed Step period.** `dt` comes from the kernel as integer nanoseconds
  and is converted once per Step. esmini's own API documents that repeatable
  results need `SE_StepDT` with what it calls a fixed timestep; the Manifest's
  declared Step period is that.
- **stdout.** The Step protocol reserves the participant's stdout for protocol
  lines; esmini logs to stdout. `_reserve_protocol_stdout` moves the real
  stdout onto a private descriptor before anything loads the library, so
  library output cannot corrupt the protocol.
- **Object identity.** Objects are named in the Manifest (`Ego=esmini.Ego`),
  resolved once through `SE_GetIdByName`, and published one object per
  Channel. esmini's object list is variable length; a Channel is not. See
  README.md for why the scenario's object count is a Manifest-time fact here.

Run it as a Manifest command:

    python3 esmini_participant.py <scenario.xosc> --object Ego=esmini.Ego \
        --object Target=esmini.Target --seed 0
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from dataclasses import dataclass

from sil.participant import ManifestError, ParticipantFailure, StepParticipant, run

NANOS_PER_SECOND = 1_000_000_000


class SE_ScenarioObjectState(ctypes.Structure):
    """`SE_ScenarioObjectState` from esminiLib.hpp, field for field.

    The header declares a plain C struct with no packing directive, so the
    platform's natural alignment — which is what ctypes applies by default —
    is the layout both sides agree on. `id_t` is `uint32_t`.
    """

    _fields_ = [
        ("id", ctypes.c_int),
        ("model_id", ctypes.c_int),
        ("ctrl_type", ctypes.c_int),
        ("timestamp", ctypes.c_double),
        ("x", ctypes.c_double),
        ("y", ctypes.c_double),
        ("z", ctypes.c_double),
        ("h", ctypes.c_double),
        ("p", ctypes.c_double),
        ("r", ctypes.c_double),
        ("roadId", ctypes.c_uint32),
        ("junctionId", ctypes.c_uint32),
        ("t", ctypes.c_double),
        ("laneId", ctypes.c_int),
        ("laneOffset", ctypes.c_double),
        ("s", ctypes.c_double),
        ("speed", ctypes.c_double),
        ("centerOffsetX", ctypes.c_double),
        ("centerOffsetY", ctypes.c_double),
        ("centerOffsetZ", ctypes.c_double),
        ("width", ctypes.c_double),
        ("length", ctypes.c_double),
        ("height", ctypes.c_double),
        ("objectType", ctypes.c_int),
        ("objectCategory", ctypes.c_int),
        ("wheel_angle", ctypes.c_double),
        ("wheel_rot", ctypes.c_double),
        ("visibilityMask", ctypes.c_int),
    ]


@dataclass(frozen=True)
class ObjectRoute:
    """One scenario object published on one Channel."""

    object_name: str
    channel: str


def _bind(library: ctypes.CDLL) -> None:
    """Declare the argument and result types of every call this adapter makes.

    ctypes defaults to `int` results and unconverted arguments, which silently
    truncates the `double` returns and mis-passes the `double` `dt` on
    x86-64. Every entry point used below is declared here instead.
    """
    library.SE_SetLogFilePath.argtypes = [ctypes.c_char_p]
    library.SE_SetLogFilePath.restype = None
    library.SE_LogToConsole.argtypes = [ctypes.c_bool]
    library.SE_LogToConsole.restype = None
    library.SE_SetSeed.argtypes = [ctypes.c_uint]
    library.SE_SetSeed.restype = None
    library.SE_Init.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.SE_Init.restype = ctypes.c_int
    library.SE_StepDT.argtypes = [ctypes.c_double]
    library.SE_StepDT.restype = ctypes.c_int
    library.SE_Close.argtypes = []
    library.SE_Close.restype = None
    library.SE_GetSimulationTime.argtypes = []
    library.SE_GetSimulationTime.restype = ctypes.c_double
    library.SE_GetQuitFlag.argtypes = []
    library.SE_GetQuitFlag.restype = ctypes.c_int
    library.SE_GetIdByName.argtypes = [ctypes.c_char_p]
    library.SE_GetIdByName.restype = ctypes.c_int
    library.SE_GetObjectState.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(SE_ScenarioObjectState),
    ]
    library.SE_GetObjectState.restype = ctypes.c_int


class EsminiParticipant(StepParticipant):
    """Drives one esmini scenario and publishes its object state."""

    def __init__(
        self,
        *,
        library_path: str,
        scenario_path: str,
        routes: list[ObjectRoute],
        seed: int,
    ):
        self._library_path = library_path
        self._scenario_path = scenario_path
        self._routes = routes
        self._seed = seed
        self._library: ctypes.CDLL | None = None
        self._object_ids: dict[str, int] = {}

    # -- initialization ----------------------------------------------------
    def on_init(self, init: dict) -> None:
        self._check_contract(init)
        self._library = self._load_library()
        _bind(self._library)
        # Both calls are documented as "before SE_Init". The log file would be
        # written into the kernel-owned Run working directory, and console
        # logging would reach the descriptor the Step protocol owns.
        self._library.SE_LogToConsole(False)
        self._library.SE_SetLogFilePath(b"")
        self._library.SE_SetSeed(self._seed)
        # disable_ctrls=0 keeps the scenario's own controllers, which is where
        # this scenario's system under test lives. use_viewer=0, threads=0 and
        # record=0 keep the Run headless, single-threaded, and free of
        # esmini's own recording.
        status = self._library.SE_Init(
            self._scenario_path.encode(), 0, 0, 0, 0
        )
        if status != 0:
            raise ManifestError(
                f"esmini SE_Init returned {status} for scenario "
                f"{self._scenario_path!r}"
            )
        self._resolve_objects()

    def _check_contract(self, init: dict) -> None:
        """Reject an init line whose Channels do not match the routes."""
        channels = init.get("channels", {})
        for route in self._routes:
            declared = channels.get(route.channel)
            if declared is None:
                raise ManifestError(
                    f"participant publishes object {route.object_name!r} on "
                    f"channel {route.channel!r}, which the Manifest does not "
                    "declare for it"
                )
            if declared.get("direction") != "out":
                raise ManifestError(
                    f"channel {route.channel!r} is declared "
                    f"{declared.get('direction')!r} for this participant, but "
                    "object state is published"
                )

    def _load_library(self) -> ctypes.CDLL:
        try:
            return ctypes.CDLL(self._library_path)
        except OSError as error:
            raise ManifestError(
                f"cannot load esmini library {self._library_path!r}: {error}"
            ) from error

    def _resolve_objects(self) -> None:
        for route in self._routes:
            object_id = self._library.SE_GetIdByName(
                route.object_name.encode()
            )
            if object_id < 0:
                raise ManifestError(
                    f"scenario {self._scenario_path!r} has no object named "
                    f"{route.object_name!r}"
                )
            self._object_ids[route.object_name] = object_id

    # -- stepping ----------------------------------------------------------
    def on_step(self, t: int, dt: int, inputs: list) -> list[tuple[str, dict]]:
        if t > 0:
            self._advance(t, dt)
        # esmini decides on its own when its scenario is over. The Manifest's
        # Duration is what decides when this Run is over. Publishing state
        # from a scenario that has already stopped would be state the
        # scenario does not stand behind, so the mismatch fails the Run
        # instead of being absorbed silently.
        if self._library.SE_GetQuitFlag() == 1:
            raise ParticipantFailure(
                f"esmini stopped its own scenario at t={t} ns, before the "
                "Manifest Duration; shorten the Duration or use a scenario "
                "that covers it"
            )
        return [
            (route.channel, self._state(route.object_name))
            for route in self._routes
        ]

    def _advance(self, t: int, dt: int) -> None:
        status = self._library.SE_StepDT(dt / NANOS_PER_SECOND)
        if status != 0:
            raise ParticipantFailure(
                f"esmini SE_StepDT returned {status} at t={t} ns"
            )

    def close(self) -> None:
        """Hand the scenario engine back before this process exits.

        The Step protocol ends with `shutdown` and the endpoint returns, which
        is where a participant holding foreign state releases it. esmini's own
        API exists for exactly this, so it is called rather than left to
        process teardown.
        """
        if self._library is not None:
            self._library.SE_Close()
            self._library = None

    def _state(self, object_name: str) -> dict:
        state = SE_ScenarioObjectState()
        status = self._library.SE_GetObjectState(
            self._object_ids[object_name], ctypes.byref(state)
        )
        if status != 0:
            raise ParticipantFailure(
                f"esmini SE_GetObjectState returned {status} for object "
                f"{object_name!r}"
            )
        return {
            "id": state.id,
            "lane_id": state.laneId,
            "x_m": state.x,
            "y_m": state.y,
            "z_m": state.z,
            "heading_rad": state.h,
            "speed_mps": state.speed,
            "s_m": state.s,
            "length_m": state.length,
            "width_m": state.width,
        }


def _reserve_protocol_stdout() -> None:
    """Give the Step protocol a descriptor esmini cannot write to.

    esmini writes to stdout from C. The protocol owns stdout. Moving the real
    stdout to a private descriptor and pointing file descriptor 1 at stderr
    keeps any library output visible as diagnostics without it ever landing
    between two protocol lines.
    """
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(protocol_fd, "w")


def _route(value: str) -> ObjectRoute:
    name, separator, channel = value.partition("=")
    if not separator or not name or not channel:
        raise argparse.ArgumentTypeError(
            f"expected <object>=<channel>, got {value!r}"
        )
    return ObjectRoute(object_name=name, channel=channel)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", help="absolute path to the .xosc scenario")
    parser.add_argument(
        "--object",
        dest="objects",
        action="append",
        type=_route,
        required=True,
        metavar="NAME=CHANNEL",
        help="publish scenario object NAME on Channel CHANNEL",
    )
    parser.add_argument(
        "--library",
        default=os.environ.get(
            "ESMINI_LIBRARY", "/opt/esmini/bin/libesminiLib.so"
        ),
        help="path to libesminiLib.so",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="seed handed to SE_SetSeed before SE_Init",
    )
    args = parser.parse_args(argv)
    _reserve_protocol_stdout()
    participant = EsminiParticipant(
        library_path=args.library,
        scenario_path=args.scenario,
        routes=args.objects,
        seed=args.seed,
    )
    try:
        run(participant)
    finally:
        participant.close()


if __name__ == "__main__":
    main()
