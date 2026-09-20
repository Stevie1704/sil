"""Read the fixture FMUs and state what driving them would require.

Three description files decide that, and this reads all three out of each
archive: `modelDescription.xml` for the co-simulation capability flags and
every variable's exact type, `extra/org.fmi-standard.fmi-ls-bus/
fmi-ls-manifest.xml` for the layered standard's name and version, and
`terminalsAndIcons/terminalsAndIcons.xml` for the bus terminals that group
those variables into a CAN channel.

It also digests the archives. A source-code FMU is a zip built at fixture
build time, so its file bytes carry a build timestamp and its own SHA-256
would change on every build while its contents did not. What is pinned here
instead is the content: each archive member's SHA-256, and one fixture digest
over the sorted member digests of all archives, which is stable across builds
and changes when anything inside an FMU changes.

    python inspect_fixture.py <fmu>... --profile profile.json

Nothing here imports SiL: this is a statement about the artifacts, taken
before any importer has an opinion about them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree

LS_MANIFEST = "extra/org.fmi-standard.fmi-ls-bus/fmi-ls-manifest.xml"
TERMINALS = "terminalsAndIcons/terminalsAndIcons.xml"
MODEL_DESCRIPTION = "modelDescription.xml"

# The layered-standard manifest's attributes are namespaced; the namespace is
# the standard's own and is part of what the fixture pins.
LS_NAMESPACE = "{http://fmi-standard.org/fmi-ls-manifest}"

# Every co-simulation capability flag FMI 3.0 defines, including the ones this
# fixture's FMUs leave at their default. A flag that is absent is reported as
# absent rather than as false: the difference is what an importer is allowed
# to assume.
CAPABILITY_FLAGS = (
    "needsExecutionTool",
    "canBeInstantiatedOnlyOncePerProcess",
    "canGetAndSetFMUState",
    "canSerializeFMUState",
    "providesDirectionalDerivatives",
    "providesAdjointDerivatives",
    "providesPerElementDependencies",
    "canHandleVariableCommunicationStepSize",
    "providesIntermediateUpdate",
    "mightReturnEarlyFromDoStep",
    "canReturnEarlyAfterIntermediateUpdate",
    "hasEventMode",
    "providesEvaluateDiscreteStates",
    "recommendedIntermediateInputSmoothness",
)

# The variable attributes that decide how a variable is driven. Whichever of
# these a variable carries is reported; the rest are not invented.
VARIABLE_ATTRIBUTES = (
    "name", "valueReference", "causality", "variability", "initial",
    "clocks", "maxSize", "mimeType", "start", "description",
    # Clock metadata. An importer needs every one of these that a Clock
    # declares, and the absence of the interval attributes is itself the
    # answer for a countdown Clock whose interval is read at run time.
    "intervalVariability", "intervalDecimal", "shiftDecimal", "priority",
    "resolution", "supportsFraction",
)


class FixtureError(ValueError):
    """An archive that is not an FMU this fixture can state a profile for."""


def member_digests(archive: Path) -> dict[str, str]:
    """Each member of one FMU archive, by SHA-256 of its bytes."""
    with zipfile.ZipFile(archive) as opened:
        return {
            name: hashlib.sha256(opened.read(name)).hexdigest()
            for name in sorted(opened.namelist())
            if not name.endswith("/")
        }


def fixture_digest(digests: dict[str, dict[str, str]]) -> str:
    """One digest over every archive's member digests, in sorted order.

    The compiled binaries are left out. They are derived from the members that
    are in — upstream's sources and descriptions — by the pinned toolchain in
    the image, and a compiler is free to vary its output without the fixture
    having changed. Their digests are still recorded per build, as an
    observation rather than as the fixture's identity.
    """
    lines = [
        f"{digest}  {archive}/{member}\n"
        for archive, members in sorted(digests.items())
        for member, digest in sorted(members.items())
        if not member.startswith("binaries/")
    ]
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def _xml(archive: Path, member: str):
    """One description file out of the archive, or None if it carries none."""
    with zipfile.ZipFile(archive) as opened:
        if member not in opened.namelist():
            return None
        return ElementTree.fromstring(opened.read(member))


def model_description(archive: Path) -> dict:
    """The capability flags, experiment defaults and variables, as declared.

    Both absences are rejected by name. This reads third-party archives, and
    an FMU without a description or without a co-simulation interface is not
    a fixture with a thin profile — it is the wrong file.
    """
    root = _xml(archive, MODEL_DESCRIPTION)
    if root is None:
        raise FixtureError(f"{archive.name} carries no {MODEL_DESCRIPTION}")
    co_simulation = root.find("CoSimulation")
    if co_simulation is None:
        raise FixtureError(
            f"{archive.name} declares no co-simulation interface"
        )
    experiment = root.find("DefaultExperiment")
    variables = []
    for variable in root.find("ModelVariables"):
        declared = {
            attribute: variable.get(attribute)
            for attribute in VARIABLE_ATTRIBUTES
            if variable.get(attribute) is not None
        }
        start = variable.find("Start")
        if start is not None:
            declared["start"] = start.get("value")
        dimension = variable.find("Dimension")
        if dimension is not None:
            declared["dimension_start"] = dimension.get("start")
        variables.append({"type": variable.tag, **declared})
    return {
        "fmiVersion": root.get("fmiVersion"),
        "modelName": root.get("modelName"),
        "instantiationToken": root.get("instantiationToken"),
        "modelIdentifier": co_simulation.get("modelIdentifier"),
        "capabilities": {
            flag: co_simulation.get(flag)
            for flag in CAPABILITY_FLAGS
            if co_simulation.get(flag) is not None
        },
        "defaultExperiment": dict(experiment.items()) if experiment is not None
        else None,
        "variables": variables,
    }


def layered_manifest(archive: Path) -> dict | None:
    """What the FMU claims about the layered standard it implements."""
    root = _xml(archive, LS_MANIFEST)
    if root is None:
        return None
    return {
        "name": root.get(f"{LS_NAMESPACE}fmi-ls-name"),
        "version": root.get(f"{LS_NAMESPACE}fmi-ls-version"),
        "isBusSimulationFMU": root.get("isBusSimulationFMU"),
        "schema": root.get(
            "{http://www.w3.org/2001/XMLSchema-instance}"
            "noNamespaceSchemaLocation"
        ),
    }


def terminals(archive: Path) -> list[dict]:
    """The bus terminals, flattened with the variables each one groups."""
    root = _xml(archive, TERMINALS)
    if root is None:
        return []
    found = []
    for terminal in root.iter("Terminal"):
        found.append({
            "name": terminal.get("name"),
            "terminalKind": terminal.get("terminalKind"),
            "matchingRule": terminal.get("matchingRule"),
            "members": [
                {
                    "memberName": member.get("memberName"),
                    "variableName": member.get("variableName"),
                    "variableKind": member.get("variableKind"),
                }
                for member in terminal.findall("TerminalMemberVariable")
            ],
        })
    return found


def binaries(archive: Path) -> list[str]:
    """The compiled platform binaries the archive carries, if any."""
    with zipfile.ZipFile(archive) as opened:
        return sorted(
            name for name in opened.namelist()
            if name.startswith("binaries/") and not name.endswith("/")
        )


def profile(archives: list[Path]) -> dict:
    """Everything the fixture states about its artifacts, in one document."""
    digests = {archive.name: member_digests(archive) for archive in archives}
    return {
        "fixture_digest": fixture_digest(digests),
        "fmus": {
            archive.name: {
                "modelDescription": model_description(archive),
                "layeredStandardManifest": layered_manifest(archive),
                "terminals": terminals(archive),
                "binaries": binaries(archive),
                "members": digests[archive.name],
            }
            for archive in archives
        },
    }


def summarize(document: dict) -> str:
    """The readable half: one block per FMU, for a person reading evidence."""
    lines = [f"fixture digest  {document['fixture_digest']}"]
    for name, fmu in document["fmus"].items():
        description = fmu["modelDescription"]
        manifest = fmu["layeredStandardManifest"]
        lines.append("")
        lines.append(f"=== {name} ===")
        lines.append(f"model            {description['modelName']} "
                     f"(FMI {description['fmiVersion']})")
        lines.append(f"layered standard {manifest['name']} "
                     f"{manifest['version']} "
                     f"(bus simulation FMU: {manifest['isBusSimulationFMU']})")
        lines.append("capabilities     " + ", ".join(
            f"{flag}={value}"
            for flag, value in description["capabilities"].items()
        ))
        lines.append("binaries         " + (
            ", ".join(fmu["binaries"]) or "none (source-code FMU)"
        ))
        lines.append("variables")
        for variable in description["variables"]:
            attributes = " ".join(
                f"{key}={value}" for key, value in variable.items()
                if key not in ("type", "name")
            )
            lines.append(f"  {variable['type']:<8} {variable['name']:<40} "
                         f"{attributes}")
        lines.append("terminals")
        for terminal in fmu["terminals"]:
            lines.append(f"  {terminal['name']} "
                         f"kind={terminal['terminalKind']} "
                         f"rule={terminal['matchingRule']}")
            for member in terminal["members"]:
                lines.append(f"    {member['memberName']} -> "
                             f"{member['variableName']} "
                             f"({member['variableKind']})")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fmus", nargs="+", type=Path)
    parser.add_argument("--profile", type=Path, required=True,
                        help="where the machine-readable profile is written")
    arguments = parser.parse_args(argv)
    document = profile(arguments.fmus)
    arguments.profile.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    print(summarize(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
