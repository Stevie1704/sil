"""The ACC example's Manifest — the single definition of the example Run.

Build it and run it:

    PYTHONPATH=python/src python examples/acc/manifest.py build/acc.json
    PYTHONPATH=python/src ./build/sil-run build/acc.json -o build/acc.mcap

`sil-run` spawns the plant as a child process, so `PYTHONPATH` has to be set on
the run as well as on the build. Both paths are under the already-ignored
build directory, so a demo leaves the working tree clean.
"""

import json
import sys
from pathlib import Path

from sil.manifest import Manifest
from sil.testing import participant_command

EXAMPLE_DIR = Path(__file__).resolve().parent
ROOT = EXAMPLE_DIR.parents[1]
ACC_SCHEMAS = json.loads((ROOT / "schemas" / "acc.json").read_text())

PLANT_STEP_PERIOD_NS = 10_000_000
DURATION_NS = 5_000_000_000


def acc_manifest() -> Manifest:
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(ACC_SCHEMAS)
    m.add_channel("acc.Sensing", schema="acc.Sensing")
    # Priority is declared rather than left implicit, even though one
    # participant cannot contend with itself: the example shows the field.
    m.add_process(
        "plant",
        command=participant_command(EXAMPLE_DIR / "plant.py", "Plant"),
        step_period_ns=PLANT_STEP_PERIOD_NS,
        publishes=["acc.Sensing"],
        priority=0,
    )
    return m


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: manifest.py <out.json>")
    print(acc_manifest().write(sys.argv[1]).hash)
