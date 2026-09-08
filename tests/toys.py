"""Shared fixtures for toy-participant manifests."""

import json

from conftest import BUILD_DIR, ROOT

from sil.manifest import Manifest

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
    **config,
):
    """Declare the toy accumulator, config and Channel contract from one source."""
    m.add_native(
        name,
        library=accumulator_library(),
        config={"input": input_channel, "output": output_channel, **config},
        subscribes=[input_channel],
        publishes=[output_channel],
    )
