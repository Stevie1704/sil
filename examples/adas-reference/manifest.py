"""The ADAS reference example's Manifest: recorded sensing into Native
instances of one C library.

Every maneuver that `prepare.py` converted gets its own Replay participant,
its own three input Channels and its own Native participant. All Native
participants load the same library, so the Run also shows that instances
keep their state apart: each must match its own expected trajectory.

What the Manifest makes explicit:

- **Periods.** A maneuver samples each sensor at its own Period: `cadence`
  publishes radar every 20 ms, camera every 40 ms and ego motion every
  10 ms. The library registers one 10 ms Task, priority 0, and rejects any
  other `period_ns`.
- **Replay order.** A Replay participant without a priority publishes the
  Messages of a Slot before every activation of that Slot, in Publish order.
  `check_experiment` rejects a replay priority: the profile does not predict
  a replay ordered among the activations.
- **Latency.** Input Channels declare `latency_ns=0`, so a Message published
  in Slot t is drained by the activation at t. One Period of Latency, the
  `late` experiment, delivers every observation one activation later; the
  profile predicts that trajectory. `check_experiment` rejects any other
  input Latency.
- **Sample time.** The activation at t advances over [t, t + 10 ms] and
  publishes, in Slot t, a Command whose `sample_time_ns` is t + 10 ms. The
  Recording stores the publication Slot t, as for an FMU output.
- **Finite routes.** Each input route holds three Messages and fails on
  overflow. A list is one Message, whatever its count. A delayed Message
  holds its place in the route from its publication until it is drained,
  and a later Message waits behind it: the `delay` experiment keeps three
  radar lists in one route. The Command Channels have no in-Run subscriber;
  they are recorded.
- **Profile.** Each Native config names the profile and its version; the
  library refuses another one.

    python manifest.py reference.json --inputs OUTDIR --library adas_reference.so
    python manifest.py drop.json --inputs OUTDIR --library adas_reference.so \
        --experiment drop
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

EXAMPLE_DIR = Path(__file__).resolve().parent
SCHEMAS = json.loads((EXAMPLE_DIR / "schemas.json").read_text())
MANEUVERS = ("clear", "hazard", "release", "unavailable", "boundaries",
             "occupancy", "turnover", "ordering", "cadence", "freshness")

PROFILE = "sil.adas-reference.radar-camera"
PROFILE_VERSION = 3
MS = 1_000_000
PERIOD_NS = 10 * MS
INPUT_LATENCY_NS = 0
# No in-Run subscriber reads a Command; one Period keeps the default unit
# delay explicit.
OUTPUT_LATENCY_NS = PERIOD_NS
INPUT_ROUTE_CAPACITY = 3
# Twenty activations, 0 ms to 190 ms; the last publishes Sample time 200 ms.
DURATION_NS = 200_000_000

PARAMETERS = {
    "hazard_acceleration_mps2": -3.0,
    "max_change_mps2": 0.5,
}

INPUTS = {"radar": "adas.ObjectList", "camera": "adas.ObjectList",
          "ego": "adas.EgoMotion"}

# The experiments over one maneuver. Each runs that maneuver alone in its own
# Manifest and is compared with maneuvers/<maneuver>.<experiment>.expected.csv.
# An Interceptor acts on an input Channel of the Run only: the authored input
# Recording and the expected trajectory stay outside every faulted path.
EXPERIMENT_MANEUVER = "cadence"
EXPERIMENTS = {
    # The camera lists sampled at 40 ms and 80 ms are lost.
    "drop": {"interceptors": {"camera": [
        {"kind": "drop", "start_ns": 40 * MS, "end_ns": 100 * MS}]}},
    # The radar lists sampled at 60 ms and 80 ms arrive 50 ms late; the lists
    # sampled at 100 ms and 120 ms wait behind the second one.
    "delay": {"interceptors": {"radar": [
        {"kind": "delay", "delay_ns": 50 * MS,
         "start_ns": 60 * MS, "end_ns": 100 * MS}]}},
    # The radar list sampled at 100 ms is invalid; the ego motion sampled at
    # 150, 160 and 170 ms repeats the sequence of the one sampled at 140 ms.
    "rewrite": {"interceptors": {
        "radar": [{"kind": "override", "field": "validity", "value": 0,
                   "start_ns": 100 * MS, "end_ns": 120 * MS}],
        "ego": [{"kind": "override", "field": "sequence", "value": 14,
                 "start_ns": 150 * MS, "end_ns": 180 * MS}]}},
    # Every observation is delivered one activation after its publication.
    "late": {"input_latency_ns": PERIOD_NS},
}
# The input Latencies whose trajectories the profile predicts.
PREDICTED_INPUT_LATENCIES_NS = (0, PERIOD_NS)


class ExperimentError(ValueError):
    """An experiment whose result the reference profile does not predict."""


def check_experiment(input_latency_ns: int,
                     replay_priority: int | None) -> None:
    """Rejects a scheduling change the profile does not predict."""
    if replay_priority is not None:
        raise ExperimentError(
            f"replay priority {replay_priority}: the replay publishes before "
            "every activation of its Slot; the profile does not predict a "
            "replay ordered among the activations")
    if input_latency_ns not in PREDICTED_INPUT_LATENCIES_NS:
        raise ExperimentError(
            f"input latency {input_latency_ns} ns: the profile predicts "
            "input Latency 0 or one controller Period "
            f"({PERIOD_NS} ns) only")


def controller_config(maneuver: str, **overrides) -> dict:
    """The Native config of one maneuver's controller."""
    return {
        "profile": PROFILE,
        "profile_version": PROFILE_VERSION,
        **{role: f"{maneuver}.{role}" for role in INPUTS},
        "command": f"{maneuver}.command",
        "period_ns": PERIOD_NS,
        **PARAMETERS,
        **overrides,
    }


# Adds one maneuver's controller to the Manifest: the maneuver, and the
# subscriber routes of its three input Channels. It publishes
# `<maneuver>.command`.
Controller = Callable[[Manifest, str, list[SubscriberRoute]], None]


def native_controller(library: Path,
                      configs: dict[str, dict] | None = None) -> Controller:
    """The controller as a Native participant of `library`. `configs`
    replaces the config of a named maneuver's controller."""
    configs = configs or {}

    def add(m: Manifest, maneuver: str,
            routes: list[SubscriberRoute]) -> None:
        m.add_native(
            maneuver,
            library=str(Path(library).resolve()),
            config=configs.get(maneuver, controller_config(maneuver)),
            subscribes=routes,
            publishes=[f"{maneuver}.command"],
        )
    return add


def process_controller(library: Path,
                       configs: dict[str, dict] | None = None,
                       python: str = "python3") -> Controller:
    """The controller as a Process participant: `process_adapter.py` loads
    `library` with ctypes in a child process of its own and takes the
    Native config as one JSON argument. `configs` replaces the config of a
    named maneuver's controller."""
    configs = configs or {}

    def add(m: Manifest, maneuver: str,
            routes: list[SubscriberRoute]) -> None:
        config = configs.get(maneuver, controller_config(maneuver))
        m.add_process(
            maneuver,
            command=[python, str(EXAMPLE_DIR / "process_adapter.py"),
                     str(Path(library).resolve()),
                     json.dumps(config, sort_keys=True)],
            step_period_ns=PERIOD_NS,
            subscribes=routes,
            publishes=[f"{maneuver}.command"],
        )
    return add


def reference_manifest(inputs: Path, library: Path | None,
                       maneuvers: tuple[str, ...] = MANEUVERS,
                       configs: dict[str, dict] | None = None,
                       interceptors: dict[str, list[dict]] | None = None,
                       input_latency_ns: int = INPUT_LATENCY_NS,
                       replay_priority: int | None = None,
                       controller: Controller | None = None,
                       duration_ns: int = DURATION_NS) -> Manifest:
    """The Run over the Recordings `prepare.py` wrote into `inputs`.

    `configs` replaces the config of a named maneuver's controller.
    `interceptors` declares, per input role, Interceptors on that input
    Channel of every maneuver. `check_experiment` rejects an input Latency or
    a replay priority the profile does not predict. `controller` substitutes
    the execution form, such as the FMU (proofs/adas-equivalence); the
    default is the Native participant of `library`. `duration_ns` lengthens
    the Run for longer inputs, such as the cost measurement
    (proofs/adas-cost)."""
    check_experiment(input_latency_ns, replay_priority)
    controller = controller or native_controller(library, configs)
    interceptors = interceptors or {}
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(SCHEMAS)
    for maneuver in maneuvers:
        for role, schema in INPUTS.items():
            m.add_channel(f"{maneuver}.{role}", schema=schema,
                          latency_ns=input_latency_ns)
            for interceptor in interceptors.get(role, ()):
                m.add_interceptor(f"{maneuver}.{role}", **interceptor)
        m.add_channel(f"{maneuver}.command", schema="adas.Command",
                      latency_ns=OUTPUT_LATENCY_NS)
        m.add_replay(
            f"{maneuver}_replay",
            recording=(Path(inputs) / f"{maneuver}.inputs.mcap").resolve(),
            channels=[f"{maneuver}.{role}" for role in INPUTS],
            priority=replay_priority,
        )
        controller(m, maneuver,
                   [SubscriberRoute(f"{maneuver}.{role}",
                                    capacity=INPUT_ROUTE_CAPACITY)
                    for role in INPUTS])
    return m


def experiment_manifest(inputs: Path, library: Path | None,
                        experiment: str,
                        controller: Controller | None = None) -> Manifest:
    """The Run of one named experiment over its maneuver."""
    return reference_manifest(inputs, library,
                              maneuvers=(EXPERIMENT_MANEUVER,),
                              controller=controller,
                              **EXPERIMENTS[experiment])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    parser.add_argument("--inputs", type=Path, required=True,
                        help="the directory prepare.py wrote")
    parser.add_argument("--library", type=Path, required=True,
                        help="the adas_reference Native library build")
    parser.add_argument("--experiment", choices=sorted(EXPERIMENTS),
                        help=f"run one experiment over {EXPERIMENT_MANEUVER}")
    args = parser.parse_args()
    m = (experiment_manifest(args.inputs, args.library, args.experiment)
         if args.experiment else reference_manifest(args.inputs, args.library))
    print(m.write(args.out).hash)
