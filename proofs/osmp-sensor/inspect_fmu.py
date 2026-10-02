"""Static compatibility report for one FMU archive: no FMU code runs here.

`describe` reads the model description and the archive's member names;
`binary` reads the shared object with binutils. `sil_gaps` names each reason
the Importer cannot drive the FMU today, with the issue that owns it.
"""
import hashlib
import re
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import osmp

INTERFACES = ("CoSimulation", "ModelExchange", "ScheduledExecution")
# FMI 2.0 names the Linux x86-64 platform directory linux64.
LINUX_X86_64 = "linux64"


def _variable(element):
    typed = next((child for child in element if child.tag != "Annotations"), None)
    if typed is None:
        raise ValueError(f"variable {element.get('name')} has no type element")
    return {"name": element.get("name"), "value_reference": int(element.get("valueReference")),
            "type": typed.tag, "causality": element.get("causality", "local"),
            "variability": element.get("variability"), "initial": element.get("initial"),
            "start": typed.get("start")}


def describe(model_description, members):
    root = ElementTree.fromstring(model_description)
    experiment = root.find("DefaultExperiment")
    variables = root.find("ModelVariables")
    return {
        "fmi_version": root.get("fmiVersion"),
        "identity": {key: root.get(key) for key in
                     ("modelName", "guid", "version", "generationTool", "generationDateAndTime")},
        "interfaces": {element.tag: dict(element.attrib)
                       for element in root if element.tag in INTERFACES},
        "default_experiment": {} if experiment is None else dict(experiment.attrib),
        "platforms": sorted({Path(name).parts[1] for name in members
                             if name.startswith("binaries/") and len(Path(name).parts) > 2}),
        "variables": [_variable(v) for v in variables.iter("ScalarVariable")],
        # FMI 2.0 has neither arrays nor clocks; FMI 3.0 would list them here.
        "arrays": [v.get("name") for v in variables if v.find("Dimension") is not None],
        "clocks": [v.get("name") for v in variables if v.tag == "Clock"],
        "osmp_binary_variables": osmp.binary_variables(model_description),
        "osmp_annotation": {key: value for tool in root.iterfind("VendorAnnotations/Tool")
                            if tool.get("name") == osmp.OSMP_TOOL
                            for element in tool for key, value in element.attrib.items()},
    }


def sil_gaps(description):
    gaps = []
    if description["fmi_version"] != "3.0":
        gaps.append(f"fmiVersion {description['fmi_version']}: "
                    "the Importer drives FMI 3.0 only (#191)")
    if description["osmp_binary_variables"]:
        gaps.append("OSMP binary variables pass memory addresses in fmi2Integer variables (#244)")
    if LINUX_X86_64 not in description["platforms"]:
        gaps.append(f"no Linux x86-64 binary (binaries/{LINUX_X86_64})")
    return gaps


def runtime_gaps(shared):
    """Protobuf linked dynamically while OSI is linked statically: every such FMU
    registers the OSI .proto files in the one process-wide Protobuf pool."""
    return [f"needs the system {library} at run time; a second FMU built this way "
            "aborts in the same process (#233, #244)"
            for library in shared["needed"] if library.startswith("libprotobuf")]


def _command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout


def binary(path):
    """Digest, machine, dependencies and symbol requirements of one shared object."""
    header = _command("readelf", "-h", path)
    dynamic = _command("readelf", "-d", path)
    versions = _command("readelf", "-V", path)
    exported = _command("nm", "-D", "--defined-only", path).splitlines()
    return {
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "size": Path(path).stat().st_size,
        "machine": re.search(r"Machine:\s+(.*)", header).group(1).strip(),
        "needed": re.findall(r"\(NEEDED\).*\[(.*)\]", dynamic),
        "required_symbol_versions": sorted(set(re.findall(r"Name: (\w+_[\d.]+)", versions))),
        "exported_fmi_functions": sorted(line.split()[-1] for line in exported
                                         if line.split()[-1].startswith("fmi2")),
        "runtime_libraries": _command("ldd", path).strip().splitlines(),
    }


def inspect(archive, directory):
    """The full static report for one archive, extracted into `directory`."""
    with zipfile.ZipFile(archive) as opened:
        members = sorted(opened.namelist())
        model_description = opened.read("modelDescription.xml").decode()
        opened.extractall(directory)
    description = describe(model_description, members)
    identifier = description["interfaces"]["CoSimulation"]["modelIdentifier"]
    shared = binary(Path(directory) / "binaries" / LINUX_X86_64 / f"{identifier}.so")
    return {"archive_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
            "members": members, "model_description": description,
            "binary": shared, "sil_gaps": sil_gaps(description) + runtime_gaps(shared)}
