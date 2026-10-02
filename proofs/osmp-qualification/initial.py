"""Read one FMU's outputs at the end of initialization, through installed SiL (#233).

Usage: initial.py <initial.json> <instance>. Prints the outputs as JSON.

A Run cannot show them: the Importer publishes an FMU's outputs only after
each `fmi2DoStep`. This script uses the installed Importer's own steps up
to that point, with the instance's Manifest bindings and start values:
extract the archive, read `modelDescription.xml`, bind the Channels,
instantiate, apply the start values, then `fmi2SetupExperiment`,
`fmi2EnterInitializationMode` and `fmi2ExitInitializationMode`. It then
reads every output-direction Channel once and terminates the FMU. No Step
runs. Each instance runs in its own process, because two of these FMUs
abort in one process.
"""
import json
import sys
from pathlib import Path

from sil.fmi.archive import Extraction
from sil.fmi.description import ModelDescription
from sil.fmi.mapping import start_values
from sil.fmi.runtime2 import CoSimulation2
from sil.fmi.single import bind_channels


def _plain(fields: dict) -> dict:
    """JSON values: a payload as the hex of its stated length."""
    if "payload_length" in fields:
        fields = {**fields, "payload": bytes(fields["payload"][:fields["payload_length"]]).hex()}
    return fields


def initial_outputs(instance: dict) -> dict:
    init = {"schemas": instance["schemas"], "channels": instance["channels"]}
    extraction = Extraction(parent=Path.cwd())
    try:
        extracted = extraction.unpack(Path(instance["fmu"]))
        description = ModelDescription.read(extracted)
        mapping = bind_channels(init, instance["binds"], description)
        fmu = CoSimulation2(description.binary(extracted), description,
                            resources=extracted / "resources")
        try:
            fmu.apply_start_values(start_values(instance["starts"], description))
            fmu.initialize()
            return {channel: _plain(binding.read(fmu))
                    for channel, binding in mapping.outputs.items()}
        finally:
            fmu.close()
    finally:
        extraction.cleanup()


if __name__ == "__main__":
    spec, name = Path(sys.argv[1]), sys.argv[2]
    print(json.dumps(initial_outputs(json.loads(spec.read_text())[name])))
