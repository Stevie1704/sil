"""Shared fixtures for toy-participant manifests."""

import json

from conftest import BUILD_DIR, ROOT

from sil.manifest import Manifest

TOY_SCHEMAS = json.loads((ROOT / "schemas" / "toy.json").read_text())


def toy_manifest(duration_ns: int = 100_000_000) -> Manifest:
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas(TOY_SCHEMAS)
    return m


def producer_library() -> str:
    return str(BUILD_DIR / "toy_producer.silp")


def accumulator_library() -> str:
    return str(BUILD_DIR / "toy_accumulator.silp")
