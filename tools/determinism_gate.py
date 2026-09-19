#!/usr/bin/env python3
"""Determinism gate over every checked-in reference manifest.

The milestone exit criterion is that a Run recorded twice produces a
bit-identical MCAP. `sil.check` proves that for one manifest; this proves it
for the reference pipeline, both variants of the ACC example, and the FMU
import example, so no example is left to be checked when someone remembers.

Run from the repository root with `python/src` on PYTHONPATH.
"""

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, "tests")

from test_determinism import full_pipeline_manifest  # noqa: E402

from sil.check import check  # noqa: E402


def load(name: str, path: str):
    """Load the FMU manifest by path so its source-relative imports work."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    build = Path("build")
    from sil.examples.acc import manifest as acc

    fmu = load("fmu_manifest", "examples/fmu/manifest.py")
    references = [
        full_pipeline_manifest(build),
        acc.acc_manifest().write(build / "acc.json"),
        acc.acc_manifest(delayed_sensing=True).write(build / "acc-delayed.json"),
        fmu.fmu_manifest().write(build / "fmu.json"),
    ]
    for reference in references:
        code = check(build / "sil-run", reference.path)
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
