"""Build the standalone FMU (Linux x86-64, Python + FMPy 0.3.32 + C++20)."""

import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import fmpy

ROOT = Path(__file__).resolve().parent
NAME = "SilCanSmoke"
MIME = 'application/org.fmi-standard.fmi-ls-bus.can; version="1.0.0"'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pack(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(
                path.relative_to(source).as_posix(), (1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def write_xml(element, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(element)
    ET.ElementTree(element).write(path, encoding="utf-8", xml_declaration=True)


def descriptions(root):
    md = ET.Element(
        "fmiModelDescription",
        fmiVersion="3.0",
        modelName=NAME,
        instantiationToken="sil-can-smoke-1",
        variableNamingConvention="structured",
    )
    ET.SubElement(
        md,
        "CoSimulation",
        modelIdentifier=NAME,
        hasEventMode="true",
        canHandleVariableCommunicationStepSize="true",
    )
    variables = ET.SubElement(md, "ModelVariables")
    ET.SubElement(
        variables,
        "Float64",
        name="time",
        valueReference="1024",
        causality="independent",
        variability="continuous",
    )
    terminals = ET.Element("fmiTerminalsAndIcons", fmiVersion="3.0")
    entries = ET.SubElement(terminals, "Terminals")
    for n in range(2):
        prefix = f"Node{n + 1}"
        terminal = ET.SubElement(
            entries,
            "Terminal",
            name=prefix,
            terminalKind="org.fmi-ls-bus.network-terminal",
            matchingRule="org.fmi-ls-bus.transceiver",
        )
        for offset, member in enumerate(("Rx_Data", "Tx_Data", "Rx_Clock", "Tx_Clock")):
            ET.SubElement(
                terminal,
                "TerminalMemberVariable",
                variableKind="signal",
                variableName=f"{prefix}.{member}",
                memberName=member,
            )
            attributes = dict(
                name=f"{prefix}.{member}", valueReference=str(4 * n + offset)
            )
            if offset < 2:
                variable = ET.SubElement(
                    variables,
                    "Binary",
                    **attributes,
                    causality="input" if offset == 0 else "output",
                    variability="discrete",
                    initial="exact" if offset == 0 else "calculated",
                    maxSize="2048",
                    clocks=str(4 * n + offset + 2),
                    mimeType=MIME,
                )
                if offset == 0:
                    ET.SubElement(variable, "Start", value="")
            else:
                ET.SubElement(
                    variables,
                    "Clock",
                    **attributes,
                    causality="input",
                    intervalVariability="triggered" if offset == 2 else "countdown",
                    **({"supportsFraction": "true"} if offset == 3 else {}),
                )
    structure = ET.SubElement(md, "ModelStructure")
    for element in ("Output", "InitialUnknown"):
        for reference in (1, 5):
            ET.SubElement(structure, element, valueReference=str(reference))
    write_xml(md, root / "modelDescription.xml")
    write_xml(terminals, root / "terminalsAndIcons/terminalsAndIcons.xml")
    ns = "http://fmi-standard.org/fmi-ls-manifest"
    ET.register_namespace("fmi-ls", ns)
    manifest = ET.Element(
        "fmiLayeredStandardManifest",
        {
            f"{{{ns}}}fmi-ls-name": "org.fmi-standard.fmi-ls-bus",
            f"{{{ns}}}fmi-ls-version": "1.0.0",
            f"{{{ns}}}fmi-ls-description": "Layered Standard for the simulation of bus communication on a Physical Signal Abstraction or Network Abstraction based level.",
            "isBusSimulationFMU": "true",
        },
    )
    write_xml(manifest, root / "extra/org.fmi-standard.fmi-ls-bus/fmi-ls-manifest.xml")


def build(destination):
    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        raise SystemExit("Build this product in the documented Linux x86-64 container")
    if fmpy.__version__ != "0.3.32":
        raise SystemExit("FMPy 0.3.32 supplies the pinned FMI headers")
    headers = Path(fmpy.__file__).parent / "c-code"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "sources"
        shutil.copytree(ROOT / "src", source)
        for name in ("fmi3Functions.h", "fmi3FunctionTypes.h", "fmi3PlatformTypes.h"):
            shutil.copy(headers / name, source)
        # Export every official entry point with its exact prototype. Unsupported
        # capabilities fail explicitly; no variadic or untyped ABI stand-ins.
        types = (source / "fmi3FunctionTypes.h").read_text()
        implemented = (source / "fmi.cpp").read_text()
        stubs = ['#include "fmi3Functions.h"', 'extern "C" {']
        for result, name, args in re.findall(
            r"typedef\s+(fmi3Status|fmi3Instance)\s+(fmi3\w+)TYPE\s*\((.*?)\);",
            types,
            re.S,
        ):
            if re.search(r"\b" + name + r"\s*\(", implemented):
                continue
            answer = "fmi3Error" if result == "fmi3Status" else "nullptr"
            stubs.append(f"{result} {name}({args}) {{ return {answer}; }}")
        stubs.append("}")
        (source / "unsupported.cpp").write_text("\n".join(stubs) + "\n")
        library = root / "binaries/x86_64-linux" / f"{NAME}.so"
        library.parent.mkdir(parents=True)
        flags = ["-std=c++20", "-O2", "-fPIC", "-shared", "-Wl,--no-undefined"]
        subprocess.run(
            [
                "c++",
                *flags,
                "-I",
                str(source),
                *map(str, sorted(source.glob("*.cpp"))),
                "-o",
                str(library),
            ],
            check=True,
        )
        descriptions(root)
        licenses = root / "documentation/licenses"
        licenses.mkdir(parents=True)
        for name in ("LICENSE", "NOTICE"):
            shutil.copy(ROOT.parents[1] / name, licenses / name)
        shutil.copy(source / "fmi3PlatformTypes.h", licenses / "FMI-BSD-2-Clause.txt")
        shutil.copy(ROOT / "README.md", root / "documentation/README.md")
        shutil.copy(Path(__file__), source / "build.py")
        (source / "build.sh").write_text(
            '#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\n'
            "mkdir -p ../binaries/x86_64-linux\n"
            "c++ -std=c++20 -O2 -fPIC -shared -Wl,--no-undefined -I. "
            "bus.cpp fmi.cpp unsupported.cpp -o ../binaries/x86_64-linux/SilCanSmoke.so\n"
        )
        resources = root / "resources"
        resources.mkdir()
        identity = {
            "profile": "sil-can-smoke-1",
            "specification": "FMI-LS-BUS 1.0.0",
            "spec_revision": "8abdf039bfb994c794e4c15bce575cfc00a1ab6e",
            "fmpy": fmpy.__version__,
            "compiler": subprocess.check_output(
                ["c++", "--version"], text=True
            ).splitlines()[0],
            "flags": flags,
            "sources": {p.name: digest(p) for p in sorted(source.iterdir())},
        }
        (resources / "identity.json").write_text(
            json.dumps(identity, indent=2, sort_keys=True) + "\n"
        )
        pack(root, destination)
    print(f"{digest(destination)}  {destination.name}")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
