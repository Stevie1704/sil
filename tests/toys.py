"""Shared manifest fixtures for the end-to-end suites."""

import json

from conftest import BUILD_DIR, COMPAT_ROUTE_CAPACITY, ROOT

from sil.manifest import Manifest, SubscriberRoute

TOY_SCHEMAS = json.loads((ROOT / "schemas" / "toy.json").read_text())


def toy_manifest(duration_ns: int = 100_000_000, *, epoch_ns: int = 0) -> Manifest:
    m = Manifest(duration_ns=duration_ns, epoch_ns=epoch_ns)
    m.add_schemas(TOY_SCHEMAS)
    return m


def producer_library() -> str:
    return str(BUILD_DIR / "toy_producer.silp")


def accumulator_library() -> str:
    return str(BUILD_DIR / "toy_accumulator.silp")


def add_producer(m, name: str = "producer", *, channel: str = "ticks", **config):
    """Declare the toy producer, config and Channel contract from one source.

    The toy reads its channel from `config`; the Manifest declares the same
    channel as an output. Deriving both here keeps them from drifting apart.
    """
    m.add_native(
        name,
        library=producer_library(),
        config={"channel": channel, **config},
        publishes=[channel],
    )


def add_accumulator(
    m,
    name: str = "acc",
    *,
    input_channel: str = "ticks",
    output_channel: str = "sums",
    route_capacity: int = COMPAT_ROUTE_CAPACITY,
    route_overflow: str = "fail",
    **config,
):
    """Declare the toy accumulator, config and Channel contract from one source."""
    m.add_native(
        name,
        library=accumulator_library(),
        config={"input": input_channel, "output": output_channel, **config},
        subscribes=[
            SubscriberRoute(
                input_channel,
                capacity=route_capacity,
                overflow=route_overflow,
            )
        ],
        publishes=[output_channel],
    )


def thrower_library() -> str:
    return str(BUILD_DIR / "toy_thrower.silp")


def add_thrower(m, name: str = "thrower", **config):
    """Declare the toy thrower, whose only job is to throw across the seam.

    Its Channel contract is empty: it publishes and subscribes to nothing.
    """
    m.add_native(
        name,
        library=thrower_library(),
        config=config,
        subscribes=[],
        publishes=[],
    )


def add_slow_subscriber(m, name: str = "slow", *, capacity: int,
                        overflow: str = "fail"):
    """Declare a subscriber that never drains, so its route fills up.

    The bench subscriber with `drain: False` is the shortest way to reach a
    bounded route's capacity from a manifest, so the bounded-route suites and
    the determinism fixtures share it.
    """
    m.add_native(
        name,
        library=str(BUILD_DIR / "bench_subscriber.silp"),
        config={"input": "ticks", "period_ns": 1_000_000_000, "drain": False},
        subscribes=[
            SubscriberRoute("ticks", capacity=capacity, overflow=overflow)
        ],
    )


def bounded_route_manifest(*, capacity: int, overflow: str = "fail"):
    """A producer publishing faster than one slow subscriber consumes."""
    m = toy_manifest(duration_ns=50_000_000)
    m.add_channel("ticks", schema="toy.Counter")
    add_producer(m, "publisher", channel="ticks", period_ns=10_000_000)
    add_slow_subscriber(m, capacity=capacity, overflow=overflow)
    return m
