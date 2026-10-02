"""The fake OSMP FMU of issue #244, as an archive a test can drive.

`tests/fixtures/fmi2_osmp.c` is the binary; this module writes the FMI 2.0
description around it, with the OSMP annotations OSI Sensor Model Packaging
1.6 declares. The fixture writes its input bytes reversed to its output.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

GUID = "{244244244-osmp}"
OSMP_TOOL = "net.pmsf.osmp"
OSMP_NAMESPACE = "http://xsd.pmsf.net/OSISensorModelPackaging"
MIME_TYPE = "application/x-open-simulation-interface; type=SensorView; version=3.8.0"

# name: (value reference, causality, OSMP binary variable, role)
VARIABLES = {
    "OSMPIn.base.lo": (0, "input", "OSMPIn", "base.lo"),
    "OSMPIn.base.hi": (1, "input", "OSMPIn", "base.hi"),
    "OSMPIn.size": (2, "input", "OSMPIn", "size"),
    "OSMPOut.base.lo": (3, "output", "OSMPOut", "base.lo"),
    "OSMPOut.base.hi": (4, "output", "OSMPOut", "base.hi"),
    "OSMPOut.size": (5, "output", "OSMPOut", "size"),
}


def annotation(binary: str, role: str, mime_type: str = MIME_TYPE) -> str:
    """The OSMP annotation of one member variable of a binary variable."""
    return (
        f'      <Annotations><Tool name="{OSMP_TOOL}" '
        f'xmlns:osmp="{OSMP_NAMESPACE}"><osmp:osmp-binary-variable '
        f'name="{binary}" role="{role}" mime-type="{mime_type}"/>'
        f"</Tool></Annotations>"
    )


def variable(name: str, reference: int, causality: str,
             annotated: str = "") -> str:
    """One fmi2Integer variable, with its annotation if it has one."""
    return (
        f'    <ScalarVariable name="{name}" valueReference="{reference}" '
        f'causality="{causality}" variability="discrete">\n'
        f'      <Integer start="0"/>\n'
        f"{annotated}\n"
        f"    </ScalarVariable>"
    )


def _declared(name: str) -> str:
    reference, causality, binary, role = VARIABLES[name]
    return variable(name, reference, causality, annotation(binary, role))


def description(model_identifier: str) -> str:
    """The FMI 2.0 description of one build of the fixture."""
    variables = "\n".join(_declared(name) for name in VARIABLES)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="Osmp" guid="{GUID}"
  generationTool="sil tests">
  <CoSimulation modelIdentifier="{model_identifier}"
    canHandleVariableCommunicationStepSize="true"/>
  <VendorAnnotations>
    <Tool name="{OSMP_TOOL}" xmlns:osmp="{OSMP_NAMESPACE}"><osmp:osmp version="1.6.0" osi-version="3.8.0"/></Tool>
  </VendorAnnotations>
  <ModelVariables>
{variables}
  </ModelVariables>
  <ModelStructure/>
</fmiModelDescription>
"""


def osmp_fmu(path: Path, binary: Path, platform_directory: str,
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
