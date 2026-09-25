"""Decode and inspect one commaCarSegments segment with opendbc's own reader.

opendbc's `rlog.capnp` is a minimal subset of openpilot's log schema. It
decodes `can`, `carParams` and `pandaStates`; every other event kind reads
as a reserved union member. The inspection therefore counts every event by
kind, so a claim that the segment has no transmit (`sendcan`) traffic rests
on every event being one of the decoded kinds, not on a name lookup.
The caller puts the pinned opendbc checkout on `sys.path`.
"""
import statistics
from collections import Counter


def _read(segment):
    from opendbc.car.logreader import LogReader
    return list(LogReader(str(segment)))


def _contract(car_params):
    safety = car_params.safetyConfigs[-1]
    return {"platform": car_params.carFingerprint,
            "mode": safety.safetyModel.raw, "mode_name": str(safety.safetyModel),
            "param": safety.safetyParam,
            "alternative_experience": car_params.alternativeExperience}


def read_events(segment):
    """The replay contract from carParams, and `can` events in time order."""
    log = _read(segment)
    car_params = next(event.carParams for event in log if event.which() == "carParams")
    events = sorted((event.logMonoTime, [(frame.address, frame.src, bytes(frame.dat))
                                         for frame in event.can])
                    for event in log if event.which() == "can")
    return _contract(car_params), events


def panda_states(segment):
    """The vehicle's own recorded safety state: (time, controls allowed, mode)."""
    return [(event.logMonoTime, bool(state.controlsAllowed), str(state.safetyModel))
            for event in _read(segment) if event.which() == "pandaStates"
            for state in event.pandaStates]


def _spread(values):
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def inspect(segment):
    log = _read(segment)
    _, events = read_events(segment)
    times = [t for t, _ in events]
    received = [[f for f in frames if f[1] < 128] for _, frames in events]
    by_address = Counter((f[1], f[0]) for frames in received for f in frames)
    span_s = (times[-1] - times[0]) / 1e9
    return {
        "event_kinds": dict(Counter(event.which() for event in log)),
        "transmit_events": sum(1 for event in log if event.which() not in
                               ("can", "carParams", "pandaStates")),
        "can_events": len(events),
        "first_log_mono_ns": times[0],
        "last_log_mono_ns": times[-1],
        "span_s": span_s,
        "ordered_as_recorded": times == [event.logMonoTime for event in log
                                         if event.which() == "can"],
        "event_interval_ns": _spread([b - a for a, b in zip(times, times[1:])]),
        "frames_per_event": _spread([len(frames) for _, frames in events]),
        "received_frames_per_event": _spread([len(frames) for frames in received]),
        "frames_by_source": {str(k): v for k, v in sorted(
            Counter(f[1] for _, frames in events for f in frames).items())},
        "payload_bytes": {str(k): v for k, v in sorted(
            Counter(len(f[2]) for _, frames in events for f in frames).items())},
        "received_addresses": [
            {"bus": bus, "address": f"{address:#x}", "frames": n, "rate_hz": round(n / span_s, 2)}
            for (bus, address), n in sorted(by_address.items())],
    }
