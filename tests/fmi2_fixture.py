"""The fake FMI 2.0 FMU of issue #191, as an archive a test can drive.

`tests/fixtures/fmi2_fake.c` is the binary; this module writes the FMI 2.0
description around it. The fixture computes

    y = gain * u        m = n + (completed steps)        c = not b

and sets the calculated parameter `offset` to `2 * gain` when it leaves
initialization mode. It reports every lifecycle call through the importer's
logger, which the importer writes to stderr as `fmu: <call> ...`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

GUID = "{191191191-fake}"

# name: (value reference, FMI 2.0 type, causality, variability, start)
VARIABLES = {
    "u": (0, "Real", "input", "continuous", "0"),
    "y": (1, "Real", "output", "continuous", None),
    "gain": (2, "Real", "parameter", "fixed", "1"),
    "offset": (3, "Real", "calculatedParameter", "fixed", None),
    "n": (4, "Integer", "input", "discrete", "0"),
    "m": (5, "Integer", "output", "discrete", None),
    "b": (6, "Boolean", "input", "discrete", "false"),
    "c": (7, "Boolean", "output", "discrete", None),
}


def _variable(name: str) -> str:
    reference, kind, causality, variability, start = VARIABLES[name]
    initial = ' initial="calculated"' if start is None else ""
    start_attribute = "" if start is None else f' start="{start}"'
    return (
        f'    <ScalarVariable name="{name}" valueReference="{reference}" '
        f'causality="{causality}" variability="{variability}"{initial}>\n'
        f"      <{kind}{start_attribute}/>\n"
        f"    </ScalarVariable>"
    )


def description(model_identifier: str, extra: str = "") -> str:
    """The FMI 2.0 description of one build of the fixture."""
    variables = "\n".join(_variable(name) for name in VARIABLES)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="Fake" guid="{GUID}"
  generationTool="sil tests">
  <CoSimulation modelIdentifier="{model_identifier}"
    canHandleVariableCommunicationStepSize="true"/>
  <ModelVariables>
{variables}
{extra}
  </ModelVariables>
  <ModelStructure/>
</fmiModelDescription>
"""


def fake_fmu(path: Path, binary: Path, platform_directory: str,
             rewrite=lambda text: text) -> Path:
    """The fixture's archive at `path`, its description passed through
    `rewrite` first. The model identifier is the binary's own name."""
    model_identifier = binary.name.split(".")[0]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "modelDescription.xml", rewrite(description(model_identifier))
        )
        archive.write(binary, f"binaries/{platform_directory}/{binary.name}")
    return path
