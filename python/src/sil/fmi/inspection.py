"""`sil-fmi-inspect`: whether this importer can drive an archive, read statically.

An inspection unpacks the archive and reads what it declares; it never loads
the binary. Every verdict it states comes from the checks a Run itself makes
before the FMU is loaded — `ModelDescription.read`, the platform binary lookup,
the single-FMU binding of `single.bind_channels` and the group's
`build_transceiver` — so a report and an initialization cannot disagree about
the same archive and the same mapping. What only a loaded binary can answer is
listed as not verified, not guessed.

The report is one of three things per archive:

- facts: what the archive declares, whether or not this importer uses it;
- `unusable`: why no Run of this importer can drive it at all;
- `unmappable` and `unsupported`, per variable and per terminal: what no
  Channel of a Run can carry. An FMU is still usable when a variable no
  Channel names is of a type this importer does not map.

A proposed mapping is the init line's `schemas` and `channels`, and the
`--bind` and `--start` arguments of the importer's command, in one document:

    {"sil_fmi_mapping": 1, "schemas": {...},
     "channels": {"<channel>": {"schema": "<schema>", "direction": "in"}},
     "bind": ["<channel>:<field>=<variable>"], "start": ["<variable>=<value>"]}
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

import sil.manifest
from sil.participant import ManifestError

from sil.fmi.archive import Extraction
from sil.fmi.composition import build_transceiver
from sil.fmi.description import (
    RX_DATA,
    ModelDescription,
    Terminal,
    Variable,
    library_suffix,
    platform_directory,
)
from sil.fmi.mapping import start_values, unclockable, unmappable
from sil.fmi.single import bind_channels
from sil.fmi.terminals import Instance

REPORT_VERSION = 1
MAPPING_VERSION = 1

# The exit status is the verdict. 2 is also what argparse exits with on a
# command line it cannot read.
EXIT_UNUSABLE = 1
EXIT_USAGE = 2
EXIT_MAPPING_REJECTED = 3

COMPATIBLE = "compatible"
UNUSABLE = "unusable"
MAPPING_REJECTED = "mapping-rejected"

_DESCRIPTION = "modelDescription.xml"
_INTERFACES = ("CoSimulation", "ModelExchange", "ScheduledExecution")
_MAPPING_KEYS = {"sil_fmi_mapping", "schemas", "channels", "bind", "start"}
_DIRECTIONS = ("in", "out")
# The causalities a Run can touch. A local, a calculated parameter and the
# independent variable are the FMU's own business.
_CARRIED = ("input", "output", "parameter", "structuralParameter")


class MappingError(ValueError):
    """A proposed mapping document that states no mapping at all."""


def inspect(archive: Path, mapping: dict | None = None) -> dict:
    """The report on one archive, and on a proposed mapping if one is given.

    `mapping` is a document `read_mapping` accepted.
    """
    with tempfile.TemporaryDirectory() as parent:
        extraction = Extraction(Path(parent))
        try:
            extracted = extraction.unpack(archive)
        except ManifestError as error:
            return _report(archive, _facts(None, None), unusable=[str(error)])
        return _inspect_extracted(archive, extracted, mapping)


def _inspect_extracted(
    archive: Path, extracted: Path, mapping: dict | None
) -> dict:
    """The report on an archive already unpacked into `extracted`."""
    root = _description_root(extracted)
    facts = _facts(extracted, root)

    def stated(error: ManifestError) -> str:
        # The extraction is gone once the report is written, so a path into
        # it is stated as a path into the archive.
        return str(error).replace(str(extracted), str(archive))

    try:
        description = ModelDescription.read(extracted)
    except ManifestError as error:
        return _report(archive, facts, unusable=[stated(error)])
    unusable = []
    try:
        description.binary(extracted)
    except ManifestError as error:
        unusable.append(stated(error))
    return _report(
        archive, facts, unusable=unusable,
        variables=_variables(root, description),
        terminals=[_terminal(description, t)
                   for t in description.terminals.values()],
        bus=None if description.bus is None
        else dataclasses.asdict(description.bus),
        unverified=_unverified(extracted, description),
        mapping=None if mapping is None else _mapping(mapping, description),
    )


def _report(archive: Path, facts: dict, *, unusable: list[str],
            variables: list[dict] = (), terminals: list[dict] = (),
            bus: dict | None = None, unverified: list[str] = (),
            mapping: dict | None = None) -> dict:
    """The report document, with the verdict its findings decide."""
    if unusable:
        verdict = UNUSABLE
    elif mapping is not None and not mapping["accepted"]:
        verdict = MAPPING_REJECTED
    else:
        verdict = COMPATIBLE
    return {
        "sil_fmi_inspection": REPORT_VERSION,
        "archive": str(archive),
        "platform": platform_directory(),
        "verdict": verdict,
        "unusable": list(unusable),
        "facts": facts,
        "variables": list(variables),
        "terminals": list(terminals),
        "bus": bus,
        "mapping": mapping,
        "unverified": list(unverified),
    }


def _description_root(extracted: Path) -> ElementTree.Element | None:
    """The description's XML root, or None where it cannot be parsed.

    The facts are read from it whatever version it declares; whether this
    importer drives it is `ModelDescription.read`'s to say.
    """
    try:
        return ElementTree.parse(extracted / _DESCRIPTION).getroot()
    except (OSError, ElementTree.ParseError):
        return None


def _facts(extracted: Path | None, root: ElementTree.Element | None) -> dict:
    """What the archive declares about itself, supported or not."""
    def attribute(name: str) -> str | None:
        return None if root is None else root.get(name)

    return {
        "fmi_version": attribute("fmiVersion"),
        "model_name": attribute("modelName"),
        "generation_tool": attribute("generationTool"),
        "instantiation_token": attribute("instantiationToken"),
        "interfaces": {} if root is None else {
            element.tag: dict(element.attrib)
            for element in root if element.tag in _INTERFACES
        },
        "platforms": [] if extracted is None else _platforms(extracted),
        "resources": extracted is not None
        and Extraction.resource_path(extracted) is not None,
    }


def _platforms(extracted: Path) -> list[str]:
    """Each `binaries/` directory that carries at least one file."""
    binaries = extracted / "binaries"
    if not binaries.is_dir():
        return []
    return sorted(
        directory.name for directory in binaries.iterdir()
        if directory.is_dir() and any(p.is_file() for p in directory.iterdir())
    )


def _variables(
    root: ElementTree.Element, description: ModelDescription
) -> list[dict]:
    """Every variable, declared as the description declares it."""
    units = {
        element.get("name"): element.get("unit")
        for element in root.iterfind("TypeDefinitions/*")
    }
    reports = []
    for element in root.find("ModelVariables"):
        variable = description.variables.get(element.get("name"))
        if variable is None:
            continue
        declared_type = element.get("declaredType")
        reports.append({
            "name": variable.name,
            "type": variable.kind,
            "value_reference": variable.reference,
            "causality": variable.causality,
            "variability": element.get("variability"),
            "start": _start(element),
            "unit": element.get("unit") or units.get(declared_type),
            "declared_type": declared_type,
            "dimensions": [
                {key: int(value) for key, value in (
                    ("start", dimension.get("start")),
                    ("value_reference", dimension.get("valueReference")),
                ) if value is not None}
                for dimension in element.findall("Dimension")
            ],
            "max_size": variable.max_size,
            "mime_type": variable.mime_type,
            "clocks": [_clock_name(description, c) for c in variable.clocks],
            "interval_variability": variable.interval_variability,
            "unmappable": (
                unclockable(variable, description) if variable.clocks
                else unmappable(variable)
            ),
        })
    return reports


def _start(element: ElementTree.Element) -> str | None:
    """A start value as declared: an attribute, or `<Start>` elements."""
    start = element.get("start")
    if start is not None:
        return start
    values = [item.get("value", "") for item in element.findall("Start")]
    return " ".join(values) if values else None


def _clock_name(description: ModelDescription, reference: int) -> str:
    """A Clock by its name, or by the reference no Clock is declared for."""
    clock = description.clock(reference)
    return clock.name if clock is not None else f"valueReference {reference}"


def _terminal(description: ModelDescription, terminal: Terminal) -> dict:
    """One terminal, checked as a group would check it.

    A group declares the profile its terminals carry; the one checked here
    is what the terminal's own `Rx_Data` states, so a terminal is reported
    unsupported for what it is, not for a profile no Run has declared yet.
    """
    profile = _media_type(description, terminal)
    instance = Instance(description.model_identifier, description)
    try:
        build_transceiver(instance, terminal, profile or "")
        unsupported = None
    except ManifestError as error:
        unsupported = str(error)
    return {
        "name": terminal.name,
        "kind": terminal.kind,
        "matching_rule": terminal.matching_rule,
        "members": dict(terminal.members),
        "bus_profile": profile,
        "unsupported": unsupported,
    }


def _media_type(
    description: ModelDescription, terminal: Terminal
) -> str | None:
    """The media type the terminal's `Rx_Data` variable carries, if any."""
    variable = description.variables.get(terminal.members.get(RX_DATA, ""))
    return None if variable is None else variable.media_type


def _unverified(extracted: Path, description: ModelDescription) -> list[str]:
    """What only a loaded binary, or its host, can answer."""
    binary = (
        f"binaries/{platform_directory()}/"
        f"{description.model_identifier}{library_suffix()}"
    )
    unverified = [
        f"whether {binary} loads on this host and the libraries it depends "
        f"on resolve",
        "whether the FMU accepts its start values and completes "
        "initialization",
    ]
    capabilities = description.capabilities
    if capabilities.get("needsExecutionTool") == "true":
        unverified.append(
            "the FMU declares needsExecutionTool=true; the tool it needs is "
            "outside the archive"
        )
    if capabilities.get("canBeInstantiatedOnlyOncePerProcess") == "true":
        unverified.append(
            "the FMU declares canBeInstantiatedOnlyOncePerProcess=true; a "
            "group that declares two instances of it shares one process"
        )
    if Extraction.resource_path(extracted) is not None:
        unverified.append(
            "the FMU reads resources/ at run time; which files it needs is "
            "not declared"
        )
    return unverified


def _mapping(mapping: dict, description: ModelDescription) -> dict:
    """The proposed mapping, checked by the checks initialization runs."""
    init = {
        "op": "init", "name": "inspection",
        "schemas": mapping["schemas"], "channels": mapping["channels"],
    }
    try:
        bind_channels(init, mapping.get("bind", []), description)
        start_values(mapping.get("start", []), description)
    except ManifestError as error:
        return {"accepted": False, "rejection": str(error), "unbound": None}
    return {"accepted": True, "rejection": None,
            "unbound": _unbound(mapping, description)}


def _unbound(mapping: dict, description: ModelDescription) -> list[str]:
    """The variables a Run could touch and this accepted mapping leaves alone.

    An unbound input or parameter keeps its start value and an unbound
    output is not published; neither is a mistake, so they are listed rather
    than rejected. A declared mapping names its variables in `bind`; a derived
    one names them as its schema fields.
    """
    if mapping.get("bind"):
        touched = {bind.partition("=")[2] for bind in mapping["bind"]}
    else:
        touched = {
            field["name"]
            for channel in mapping["channels"].values()
            for field in mapping["schemas"][channel["schema"]]["fields"]
        }
    touched |= {start.partition("=")[0] for start in mapping.get("start", [])}
    return [
        name for name, variable in description.variables.items()
        if _carried(variable) and name not in touched
    ]


def _carried(variable: Variable) -> bool:
    return variable.causality in _CARRIED


def read_mapping(path: Path) -> dict:
    """A proposed mapping document, checked for the shape a Run gives it."""
    def refuse(problem: str) -> MappingError:
        return MappingError(f"mapping {str(path)!r} {problem}")

    try:
        document = json.loads(path.read_text())
    except OSError as error:
        raise MappingError(f"cannot read mapping {str(path)!r}: {error}")
    except json.JSONDecodeError as error:
        raise refuse(f"is not JSON: {error}")
    if not isinstance(document, dict):
        raise refuse("is not a JSON object")
    if document.get("sil_fmi_mapping") != MAPPING_VERSION:
        raise refuse(
            f"declares sil_fmi_mapping {document.get('sil_fmi_mapping')!r}; "
            f"this command reads version {MAPPING_VERSION}"
        )
    for key in document:
        if key not in _MAPPING_KEYS:
            raise refuse(f"has unknown key {key!r}")
    try:
        # The Manifest's own schema rules, so a schema a Run would refuse
        # is not inspected as if it were one.
        sil.manifest.Manifest(duration_ns=1).add_schemas(
            document.get("schemas", {})
        )
    except sil.manifest.ManifestError as error:
        raise refuse(f"declares a schema a Manifest refuses: {error}") from error
    schemas = document.setdefault("schemas", {})
    channels = document.setdefault("channels", {})
    if not isinstance(channels, dict):
        raise refuse("key 'channels' is not an object")
    for name, channel in channels.items():
        problem = _channel_problem(channel, schemas)
        if problem is not None:
            raise refuse(f"channel {name!r} {problem}")
    for key in ("bind", "start"):
        arguments = document.get(key, [])
        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise refuse(f"key {key!r} is not a list of strings")
    return document


def _channel_problem(channel: object, schemas: dict) -> str | None:
    """What is wrong with one channel declaration, or None."""
    if not isinstance(channel, dict) or set(channel) != {"schema", "direction"}:
        return 'is not {"schema": ..., "direction": ...}'
    if channel["schema"] not in schemas:
        return f"names unknown schema {channel['schema']!r}"
    if channel["direction"] not in _DIRECTIONS:
        return (
            f"declares direction {channel['direction']!r}; a direction is "
            f"'in' or 'out'"
        )
    return None


def render(report: dict) -> str:
    """The report as a person reads it."""
    facts = report["facts"]
    lines = [
        f"archive: {report['archive']}",
        f"verdict: {report['verdict']}",
        f"fmiVersion: {facts['fmi_version']}",
        f"model: {facts['model_name']} ({facts['generation_tool']})",
        f"interfaces: {', '.join(facts['interfaces']) or 'none'}",
        f"platform binaries: {', '.join(facts['platforms']) or 'none'} "
        f"(this host: {report['platform']})",
    ]
    co_simulation = facts["interfaces"].get("CoSimulation")
    if co_simulation:
        lines.append("co-simulation capabilities: " + ", ".join(
            f"{name}={value}" for name, value in co_simulation.items()
        ))
    lines += _section("unusable", report["unusable"])
    if report["variables"]:
        lines.append(f"variables ({len(report['variables'])}):")
        lines += [_render_variable(v) for v in report["variables"]]
    if report["bus"] is not None:
        lines.append(
            f"FMI-LS-BUS {report['bus']['version']}, "
            f"isBusSimulationFMU={str(report['bus']['bus_simulation']).lower()}"
        )
    if report["terminals"]:
        lines.append(f"terminals ({len(report['terminals'])}):")
        for terminal in report["terminals"]:
            state = terminal["unsupported"] or "a group connects it"
            lines.append(
                f"  {terminal['name']} [{terminal['bus_profile']}]: {state}"
            )
    mapping = report["mapping"]
    if mapping is not None:
        if mapping["accepted"]:
            lines.append("mapping: accepted")
            lines += _section("left unbound by the mapping", mapping["unbound"])
        else:
            lines.append(f"mapping: rejected: {mapping['rejection']}")
    lines += _section("not verified statically", report["unverified"])
    return "\n".join(lines) + "\n"


def _section(title: str, items: list[str]) -> list[str]:
    """A titled list, or nothing when the list is empty."""
    return [f"{title}:"] + [f"  - {item}" for item in items] if items else []


def _render_variable(variable: dict) -> str:
    """One variable on one line: its declaration, then the verdict."""
    declared = [variable["type"], variable["causality"]]
    for key in ("variability", "start", "unit", "max_size", "mime_type",
                "interval_variability"):
        if variable[key] is not None:
            declared.append(f"{key}={variable[key]}")
    if variable["dimensions"]:
        declared.append(f"dimensions={variable['dimensions']}")
    if variable["clocks"]:
        declared.append(f"clocks={','.join(variable['clocks'])}")
    state = (
        f"not mappable, {variable['unmappable']}" if variable["unmappable"]
        else "mappable"
    )
    return f"  {variable['name']}: {' '.join(declared)} — {state}"


def exit_status(report: dict) -> int:
    """The documented exit code for a report's verdict."""
    return {
        COMPATIBLE: 0,
        UNUSABLE: EXIT_UNUSABLE,
        MAPPING_REJECTED: EXIT_MAPPING_REJECTED,
    }[report["verdict"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sil-fmi-inspect",
        description="Report whether the FMI importer can drive an FMU, "
                    "without loading the FMU's binary.",
        allow_abbrev=False,
    )
    parser.add_argument("fmu", type=Path, help="path to the FMU archive")
    parser.add_argument(
        "--mapping", type=Path, default=None,
        help="a proposed mapping document (sil_fmi_mapping 1) to check "
             "against the archive",
    )
    parser.add_argument("--json", action="store_true",
                        help="write the report as JSON")
    args = parser.parse_args(argv)
    try:
        mapping = None if args.mapping is None else read_mapping(args.mapping)
    except MappingError as error:
        sys.stderr.write(f"sil-fmi-inspect: {error}\n")
        return EXIT_USAGE
    report = inspect(args.fmu, mapping)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(render(report))
    return exit_status(report)


if __name__ == "__main__":
    sys.exit(main())
