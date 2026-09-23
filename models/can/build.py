"""Build the standalone FMU (Linux x86-64, Python + FMPy 0.3.32 + C++20)."""

import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import fmpy

from build_support import ROOT, PROFILE, LAYOUT, digest, pack

NAME = PROFILE["model_name"]
MIME = PROFILE["mime_type"]


def write_xml(element, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(element)
    ET.ElementTree(element).write(path, encoding="utf-8", xml_declaration=True)


def write_descriptions(root):
    md = ET.Element(
        "fmiModelDescription",
        fmiVersion="3.0",
        modelName=NAME,
        instantiationToken=PROFILE["token"],
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
        valueReference=str(LAYOUT["time"]),
        causality="independent",
        variability="continuous",
    )
    for name, reference, default in (
        ("activeNodeCount", LAYOUT["active_nodes"], "2"),
        ("perNodeQueueCapacity", LAYOUT["queue_capacity"], "4"),
    ):
        ET.SubElement(
            variables, "Float64", name=name, valueReference=str(reference),
            causality="parameter", variability="fixed", start=default,
        )
    terminals = ET.Element("fmiTerminalsAndIcons", fmiVersion="3.0")
    entries = ET.SubElement(terminals, "Terminals")
    for n in range(LAYOUT["terminal_count"]):
        prefix = f"Node{n + 1}"
        terminal = ET.SubElement(
            entries,
            "Terminal",
            name=prefix,
            terminalKind="org.fmi-ls-bus.network-terminal",
            matchingRule="org.fmi-ls-bus.transceiver",
        )
        for offset, member in (
            (LAYOUT[name.lower()], name)
            for name in ("Rx_Data", "Tx_Data", "Rx_Clock", "Tx_Clock")
        ):
            ET.SubElement(
                terminal,
                "TerminalMemberVariable",
                variableKind="signal",
                variableName=f"{prefix}.{member}",
                memberName=member,
            )
            attributes = dict(
                name=f"{prefix}.{member}",
                valueReference=str(LAYOUT["terminal_stride"] * n + offset),
            )
            if member.endswith("Data"):
                variable = ET.SubElement(
                    variables,
                    "Binary",
                    **attributes,
                    causality="input" if member == "Rx_Data" else "output",
                    variability="discrete",
                    initial="exact" if member == "Rx_Data" else "calculated",
                    maxSize=str(LAYOUT["max_binary_size"]),
                    clocks=str(
                        LAYOUT["terminal_stride"] * n
                        + LAYOUT[member.replace("Data", "Clock").lower()]
                    ),
                    mimeType=MIME,
                )
                if member == "Rx_Data":
                    ET.SubElement(variable, "Start", value="")
            else:
                ET.SubElement(
                    variables,
                    "Clock",
                    **attributes,
                    causality="input",
                    intervalVariability="triggered"
                    if member == "Rx_Clock"
                    else "countdown",
                    **({"supportsFraction": "true"} if member == "Tx_Clock" else {}),
                )
    structure = ET.SubElement(md, "ModelStructure")
    for element in ("Output", "InitialUnknown"):
        for n in range(LAYOUT["terminal_count"]):
            reference = LAYOUT["terminal_stride"] * n + LAYOUT["tx_data"]
            ET.SubElement(
                structure,
                element,
                valueReference=str(reference),
                **({"dependencies": ""} if element == "InitialUnknown" else {}),
            )
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
        declarations = [
            f"inline constexpr char token[] = {json.dumps(PROFILE['token'])};"
        ]
        declarations += [
            f"inline constexpr unsigned {name} = {value};"
            for name, value in LAYOUT.items()
        ]
        (source / "profile.hpp").write_text(
            "#pragma once\nnamespace profile {\n" + "\n".join(declarations) + "\n}\n"
        )
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
        compile_command = [
            "c++",
            *flags,
            "-I.",
            *(p.name for p in sorted(source.glob("*.cpp"))),
            "-o",
            f"../binaries/x86_64-linux/{NAME}.so",
        ]
        subprocess.run(compile_command, cwd=source, check=True)
        write_descriptions(root)
        licenses = root / "documentation/licenses"
        licenses.mkdir(parents=True)
        for name in ("LICENSE", "NOTICE"):
            shutil.copy(ROOT.parents[1] / name, licenses / name)
        shutil.copy(source / "fmi3PlatformTypes.h", licenses / "FMI-BSD-2-Clause.txt")
        shutil.copy(ROOT / "README.md", root / "documentation/README.md")
        shutil.copy(Path(__file__), source / "build.py")
        shutil.copy(ROOT / "profile.json", source)
        shutil.copy(ROOT / "build_support.py", source)
        (source / "build.sh").write_text(
            '#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\n'
            "mkdir -p ../binaries/x86_64-linux\n" + shlex.join(compile_command) + "\n"
        )
        resources = root / "resources"
        resources.mkdir()
        identity = {
            "profile": PROFILE["token"],
            "specification": PROFILE["specification"],
            "spec_revision": PROFILE["upstream"]["spec"]["revision"],
            "git_revision": subprocess.check_output(
                [
                    "git",
                    "-c",
                    f"safe.directory={ROOT.parents[1]}",
                    "-C",
                    str(ROOT.parents[1]),
                    "rev-parse",
                    "HEAD",
                ],
                text=True,
            ).strip(),
            "source_dirty": bool(
                subprocess.check_output(
                    [
                        "git",
                        "-c",
                        f"safe.directory={ROOT.parents[1]}",
                        "-C",
                        str(ROOT.parents[1]),
                        "status",
                        "--porcelain",
                        "--",
                        "models/can",
                        "LICENSE",
                        "NOTICE",
                    ],
                    text=True,
                ).strip()
            ),
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
