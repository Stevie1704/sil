"""Edge Participants of the mixed closed loop (issue #227).

They connect the ADAS reference controller to the ACC plant FMU. Each one is
a Process participant; `loop.py` states their Periods, priorities, Channels
and Latencies, and `docs` of this directory the whole consumption table.

- `Maneuver` publishes the authored lead-vehicle Maneuver: the lead
  acceleration the plant applies over [t, t + 10 ms], and whether the lead
  is visible to the sensors at t. It is part of the Run contract, not of a
  sensor Channel.
- `Sensor` derives processed observations from plant truth: an idealized
  radar or camera object list, or the ego motion. One object stands for the
  lead vehicle; there is no camera or radar physics, noise, range or field
  of view.
- `Actuator` converts the controller's Command into the plant's input.

The plant interface and the reference profile differ in their types: the
plant reads and writes Float64, the profile carries Float32. The importer
refuses to bind a Float32 field to a Float64 variable, so the conversion is
explicit here: `to_f32` rounds a plant value once, to the nearest binary32,
and refuses one that is not finite or does not fit; the actuator widens the
binary32 acceleration to binary64, which is exact. Units are SI on both
sides, so no value is scaled.

No Participant here consumes a future observation. A truth Message published
in Slot s describes s + 10 ms (the importer's Sample time); a sensor at t
takes the truth sampled at t and fails on any other. The actuator at t takes
the Command whose Sample time is t and fails on any other.

    python edge.py ROLE CONFIG_JSON
"""

from __future__ import annotations

import json
import math
import struct
import sys

from sil.participant import ManifestError, ParticipantFailure, StepParticipant, run

CAPACITY = 8
EGO_FRAME_ID = 1
SENSOR_IDS = {"radar": 1, "camera": 2}
TRUTH_FIELDS = ("ego_position_m", "ego_speed_mps", "lead_position_m",
                "lead_speed_mps", "gap_m", "relative_speed_mps")


class ConversionError(ParticipantFailure):
    """A plant value the profile cannot carry."""


def to_f32(name: str, value: float) -> float:
    """`value` rounded once to the nearest binary32, ties to even.

    A value that is not finite, or that rounds outside binary32, is refused:
    it is never clamped or replaced."""
    if not math.isfinite(value):
        raise ConversionError(f"{name} {value!r} is not finite")
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError as error:
        raise ConversionError(
            f"{name} {value!r} does not fit binary32") from error


def object_list(sensor: str, t: int, sequence: int, truth: dict,
                visible: bool, config: dict) -> dict:
    """One sensor's list at Sample time t: the lead, when visible, as one
    object at the plant gap, straight ahead; inactive elements zero."""
    objects = []
    if visible:
        objects.append({
            "object_id": config["object_id"],
            "x_m": to_f32(f"{sensor}.x_m", truth["gap_m"]),
            "y_m": 0.0,
            # The camera reports no speed (profile 3).
            "relative_vx_mps": (to_f32(f"{sensor}.relative_vx_mps",
                                       truth["relative_speed_mps"])
                                if sensor == "radar" else 0.0),
            "confidence": to_f32(f"{sensor}.confidence",
                                 config["confidence"]),
        })
    arrays = {name: [o[name] for o in objects]
              + [0] * (CAPACITY - len(objects))
              for name in ("object_id", "x_m", "y_m", "relative_vx_mps",
                           "confidence")}
    return {"sample_time_ns": t, "sensor_id": SENSOR_IDS[sensor],
            "frame_id": EGO_FRAME_ID, "sequence": sequence,
            "count": len(objects), "validity": 1, **arrays}


def ego_motion(t: int, sequence: int, truth: dict) -> dict:
    speed = to_f32("ego.speed_mps", truth["ego_speed_mps"])
    if speed < 0:
        # Profile 3 has no negative ego speed; the plant has no speed floor.
        raise ConversionError(f"ego.speed_mps {speed!r} is negative")
    return {"sample_time_ns": t, "sequence": sequence, "validity": 1,
            "speed_mps": speed}


def _config(text: str, keys: set[str]) -> dict:
    """The participant's config: exactly `keys`, or a Manifest error."""
    try:
        config = json.loads(text)
    except json.JSONDecodeError as error:
        raise ManifestError(f"config is not JSON: {error}") from error
    if not isinstance(config, dict) or set(config) != keys:
        got = sorted(config) if isinstance(config, dict) else config
        raise ManifestError(f"config keys {got}, expected {sorted(keys)}")
    return config


class Maneuver(StepParticipant):
    """The authored lead acceleration and lead visibility at each Step."""

    KEYS = {"lead", "visibility", "lead_accel", "hidden"}

    def __init__(self, config: dict):
        self.config = config

    def on_step(self, t, dt, inputs):
        c = self.config
        accel = sum(a for start, end, a in c["lead_accel"] if start <= t < end)
        visible = not any(start <= t < end for start, end in c["hidden"])
        return [(c["lead"], {"lead_accel_mps2": float(accel)}),
                (c["visibility"], {"sample_time_ns": t,
                                   "lead_visible": int(visible)})]


class Sensor(StepParticipant):
    """Processed observations of plant truth at the sensor's Period.

    Before the first truth Message, the truth at time 0 is the Run's
    declared initial condition. After it, the truth sampled at t must have
    been delivered."""

    KEYS = {"sensor", "truth", "truth_sample_offset_ns", "visibility",
            "output", "initial_truth", "object_id", "confidence"}

    def __init__(self, config: dict):
        self.config = config
        if set(config["initial_truth"]) != set(TRUTH_FIELDS) or not all(
                isinstance(v, (int, float)) and math.isfinite(v)
                for v in config["initial_truth"].values()):
            raise ManifestError(
                f"initial_truth must give finite {', '.join(TRUTH_FIELDS)}")
        self.truth = (0, dict(config["initial_truth"]))
        self.sequence = 0

    def on_step(self, t, dt, inputs):
        c = self.config
        visibility = None
        # A route delivers in Publish order: the last Message is the newest.
        for message in inputs:
            if message.channel == c["truth"]:
                self.truth = (message.publish_ns + c["truth_sample_offset_ns"],
                              message.data)
            elif message.channel == c["visibility"]:
                visibility = message.data
        if c["visibility"] is not None and (
                visibility is None or visibility["sample_time_ns"] != t):
            raise ParticipantFailure(
                f"t={t} ns: {c['sensor']} has no visibility sampled at t")
        sampled, truth = self.truth
        if sampled != t:
            when = "after" if sampled > t else "before"
            raise ParticipantFailure(
                f"t={t} ns: {c['sensor']} holds truth sampled at {sampled} ns, "
                f"{when} the activation time; a sensor describes only truth "
                "sampled at its activation")
        if c["sensor"] == "ego":
            fields = ego_motion(t, self.sequence, truth)
        else:
            fields = object_list(c["sensor"], t, self.sequence, truth,
                                 bool(visibility["lead_visible"]), c)
        self.sequence += 1
        return [(c["output"], fields)]


class Actuator(StepParticipant):
    """The Command's acceleration, widened to the plant's Float64 input.

    At t it takes the Command whose Sample time is t: the acceleration the
    controller commands for the interval [t, t + 10 ms]. Before the first
    Command it applies the declared initial acceleration."""

    KEYS = {"command", "actuation", "initial_accel_mps2"}

    def __init__(self, config: dict):
        self.config = config
        initial = config["initial_accel_mps2"]
        if isinstance(initial, bool) or not isinstance(initial, (int, float)) \
                or not math.isfinite(initial):
            raise ManifestError(
                f"initial_accel_mps2 {initial!r} must be a finite number")
        self.first = True

    def on_step(self, t, dt, inputs):
        c = self.config
        commands = [m.data for m in inputs if m.channel == c["command"]]
        if self.first:
            self.first = False
            if commands:
                raise ParticipantFailure(
                    f"t={t} ns: a Command before the first activation")
            accel = float(c["initial_accel_mps2"])
        else:
            if len(commands) != 1 or commands[0]["sample_time_ns"] != t:
                got = [cmd["sample_time_ns"] for cmd in commands]
                raise ParticipantFailure(
                    f"t={t} ns: Commands sampled at {got} ns; the actuator "
                    "takes exactly the one sampled at the activation time")
            # binary32 to binary64 is exact.
            accel = float(commands[0]["acceleration_mps2"])
        return [(c["actuation"], {"accel_mps2": accel})]


ROLES = {"maneuver": Maneuver, "sensor": Sensor, "actuator": Actuator}


class _Deferred(StepParticipant):
    """Reads the config in `on_init`, so a bad one answers `fail` before
    `ready`: a Manifest error (exit 2), not a Run failure."""

    def __init__(self, role: str, text: str):
        self.role, self.text = role, text
        self.participant: StepParticipant | None = None

    def on_init(self, init):
        cls = ROLES.get(self.role)
        if cls is None:
            raise ManifestError(f"unknown edge role {self.role!r}")
        self.participant = cls(_config(self.text, cls.KEYS))

    def on_step(self, t, dt, inputs):
        return self.participant.on_step(t, dt, inputs)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: edge.py ROLE CONFIG_JSON")
    run(_Deferred(sys.argv[1], sys.argv[2]))
