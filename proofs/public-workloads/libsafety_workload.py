"""opendbc's safety library driven by one recorded CAN segment.

This is the standalone execution a SiL adapter is compared with. The library
is the upstream C safety logic compiled once, during preparation, by the
upstream harness's own build function. It is loaded through the upstream CFFI
declarations and never rebuilt here. Every policy the driver applies is named
below; an adapter that changes one of them is a different workload:

- order: `can` events by `logMonoTime`; frames in recorded order within one;
- time: `set_timer(timer_us(logMonoTime))` before each event's frames;
- receive side only: frames from a transmit echo source (>= 128) are skipped;
- warm-up: `safety_tick` runs only more than one second from either end;
- initial state: `set_safety_hooks(mode, param)` and the recorded alternative
  experience, then nothing else. No steering state is seeded: the segment
  carries no `sendcan`, so upstream seeding does not apply.

The library keeps its state in C globals. A second `dlopen` of the same file
in one process returns the same state, so one instance needs one process.

Usage: libsafety_workload.py <libsafety.so> <opendbc> <segment> <variant> <out.json>
"""
import json
import sys
from collections import Counter

TRANSMIT_ECHO_SOURCE = 128
WARM_UP_NS = 1_000_000_000
STATE = ("controls_allowed", "gas_pressed_prev", "brake_pressed_prev",
         "cruise_engaged_prev", "vehicle_moving", "acc_main_on",
         "vehicle_speed_min", "vehicle_speed_max")


def timer_us(log_mono_ns):
    """Upstream replay's counter, including its modulus of 0xFFFFFFFF."""
    return (log_mono_ns // 1000) % 0xFFFFFFFF


def ticks(t, first, last):
    return t - first > WARM_UP_NS and last - t > WARM_UP_NS


def received(frames):
    return [frame for frame in frames if frame[1] < TRANSMIT_ECHO_SOURCE]


def first_divergence(reference, candidate):
    """The first row or field where two state traces differ, or None."""
    for index, (expected, actual) in enumerate(zip(reference, candidate)):
        for field in expected:
            if expected[field] != actual.get(field):
                return {"index": index, "t_ns": expected["t_ns"], "field": field,
                        "expected": expected[field], "actual": actual.get(field)}
    if len(reference) != len(candidate):
        return {"index": min(len(reference), len(candidate)), "field": "coverage",
                "expected": len(reference), "actual": len(candidate)}
    return None


# Deliberately wrong variants. Each must be detected by the reference check.
VARIANTS = {
    "nominal": {},
    # Unit error: nanoseconds injected where the library expects microseconds.
    "timer-in-ns": {"timer": lambda t: t % 0xFFFFFFFF},
    # Input error: the last byte (Toyota checksum) of the steering torque
    # sensor frame 0x260 on bus 0 is inverted.
    "corrupt-0x260": {"corrupt": (0x260, 0)},
}


def replay(lib, packet, contract, events, timer=timer_us, corrupt=None):
    if lib.set_safety_hooks(contract["mode"], contract["param"]) != 0:
        raise RuntimeError(f"safety mode {contract['mode']} param {contract['param']} rejected")
    lib.set_alternative_experience(contract["alternative_experience"])
    first, last = events[0][0], events[-1][0]
    trace, rejected, config_valid = [], Counter(), True
    for t, frames in events:
        lib.set_timer(timer(t))
        if ticks(t, first, last):
            lib.safety_tick()
            config_valid &= bool(lib.safety_config_valid())
        accepted = refused = 0
        for address, source, data in received(frames):
            if corrupt == (address, source):
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            lib.safety_fwd_hook(source, address)
            if lib.safety_rx_hook(packet(address, source % 4, data)):
                accepted += 1
            else:
                refused += 1
                rejected[f"{source}:{address:#x}"] += 1
        state = {name: getattr(lib, f"get_{name}")() for name in STATE}
        trace.append({"t_ns": t, "accepted": accepted, "rejected": refused,
                      **{k: v if isinstance(v, float) else bool(v) for k, v in state.items()}})
    return {"trace": trace, "rejected": dict(rejected), "config_valid": config_valid}


def load(library, opendbc):
    sys.path.insert(0, opendbc)
    from opendbc.safety.tests.libsafety import libsafety_py
    libsafety_py.load(library)
    return libsafety_py


def shared_state_across_same_path_loads(libsafety_py, library):
    first = libsafety_py.libsafety
    second = libsafety_py.ffi.dlopen(library)
    first.set_controls_allowed(False)
    second.set_controls_allowed(True)
    shared = bool(first.get_controls_allowed())
    first.set_controls_allowed(False)
    return shared


def main(library, opendbc, segment, variant, output):
    from can_recording import read_events
    libsafety_py = load(library, opendbc)
    contract, events = read_events(segment)
    result = replay(libsafety_py.libsafety, libsafety_py.make_CANPacket, contract,
                    events, **VARIANTS[variant])
    result["variant"] = variant
    result["contract"] = contract
    if variant == "nominal":
        result["same_path_dlopen_shares_state"] = shared_state_across_same_path_loads(
            libsafety_py, library)
    with open(output, "w") as out:
        json.dump(result, out, allow_nan=False)


if __name__ == "__main__":
    main(*sys.argv[1:])
