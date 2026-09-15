"""The FMU example's Manifest — the single definition of the example Run.

Two participants. `stimulus` publishes `fmu.In`; the importer subscribes to it,
writes its fields into the FMU's input variables, steps the FMU, and publishes
its output variables on `fmu.Out`. The kernel is not told that either
participant is special, and the importer is declared the way any other process
participant is: a command and its arguments.

The FMU is the Modelica Association's `Feedthrough` Reference FMU, vendored
under `tests/fixtures/reference-fmus/`. It copies each input to the output of
the same name, so the Recording shows the mapping round-trip directly: an
output at `t` is the input published one Step earlier. That Latency is the
Channel's, not the FMU's — a Message published at `t` is visible at the
subscriber's next activation.

Variable mapping needs no configuration of its own. A Channel's schema field
names *are* the FMU's variable names, and Channel direction decides which side
of the Step a variable is touched on. The FMU path travels as a command
argument, which the Manifest already hashes, so nothing that affects the Run
lives outside the hashed Manifest.

Build it and run it:

    make example-fmu

or, without the convenience target:

    PYTHONPATH=$PWD/python/src python examples/fmu/manifest.py build/fmu.json
    PYTHONPATH=$PWD/python/src ./build/sil-run build/fmu.json -o build/fmu.mcap

`sil-run` spawns the participants as child processes, so `PYTHONPATH` has to be
set on the run as well as on the build, and absolute: the kernel starts each
child in its own kernel-owned working directory, so a relative entry would be
resolved from there. The importer extracts the archive into that directory,
which the kernel removes after the Run, so a demo leaves the working tree
clean.

To point this at your own FMU, change three things and nothing else: the path
below, the schemas to carry your FMU's Float64 input and output variable names,
and `stimulus.py` to publish what your FMU needs. Float64 `input` and `output`
variables are what this milestone maps; see the README for the boundaries.
"""

import argparse
import json
import sys
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute
from sil.testing import participant_command

EXAMPLE_DIR = Path(__file__).resolve().parent
ROOT = EXAMPLE_DIR.parents[1]
FMU_SCHEMAS = json.loads((ROOT / "schemas" / "fmu.json").read_text())

FMU_PATH = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"

STEP_PERIOD_NS = 10_000_000
DURATION_NS = 1_000_000_000

# One Message per Step on a route that is drained every Step, plus the one the
# Channel's default Latency holds. The importer is the only subscriber.
ROUTE_CAPACITY = 2


def fmu_manifest(*, fmu: Path = FMU_PATH) -> Manifest:
    """The example Run. `fmu` is the archive the importer drives."""
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(FMU_SCHEMAS)
    m.add_channel("fmu.In", schema="fmu.In")
    m.add_channel("fmu.Out", schema="fmu.Out")
    m.add_process(
        "stimulus",
        command=participant_command(EXAMPLE_DIR / "stimulus.py", "Stimulus"),
        step_period_ns=STEP_PERIOD_NS,
        publishes=["fmu.In"],
    )
    # The importer: `sil.fmi` is a module in the Python package, named as an
    # ordinary process participant's command with the FMU path after it.
    # `priority` puts it after the stimulus in the same Step.
    m.add_process(
        "fmu",
        command=[sys.executable, "-m", "sil.fmi", str(fmu)],
        step_period_ns=STEP_PERIOD_NS,
        subscribes=[SubscriberRoute("fmu.In", capacity=ROUTE_CAPACITY)],
        publishes=["fmu.Out"],
        priority=1,
    )
    return m


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", help="path to write the Manifest to")
    args = parser.parse_args()
    print(fmu_manifest().write(args.out).hash)
