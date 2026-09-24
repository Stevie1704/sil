"""Build an explicitly adapted upstream 1.0.0 sender and receive-only peer."""

import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import fmpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import PROFILE, digest, pack

EXAMPLES = PROFILE["upstream"]["examples"]["revision"]
SPEC = PROFILE["upstream"]["spec"]["revision"]
PATCH = Path(__file__).with_name("node.patch")
FAULT_PATCH = Path(__file__).with_name("fault-aware-node.patch")


def build(checkout, spec, output):
    for path, revision in ((checkout, EXAMPLES), (spec, SPEC)):
        actual = subprocess.check_output(
            [
                "git",
                "-c",
                f"safe.directory={path}",
                "-C",
                str(path),
                "rev-parse",
                "HEAD",
            ],
            text=True,
        ).strip()
        if actual != revision:
            raise SystemExit(f"{path}: expected revision {revision}, got {actual}")
    headers = Path(fmpy.__file__).parent / "c-code"
    for receiver, fault_aware in (
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sources"
            shutil.copytree(checkout / "can-node-triggered-output/src", source)
            subprocess.run(
                ["patch", "-p1", "-i", str(PATCH.resolve())], cwd=source, check=True
            )
            if fault_aware:
                subprocess.run(
                    ["patch", "-p1", "-i", str(FAULT_PATCH.resolve())],
                    cwd=source,
                    check=True,
                )
            for path in headers.glob("fmi3*.h"):
                shutil.copy(path, source)
            for path in (spec / "headers").glob("*.h"):
                shutil.copy(path, source)
            descriptions = checkout / "can-node-triggered-output/description"
            shutil.copy(descriptions / "modelDescription.xml", root)
            for directory, filename in (
                ("terminalsAndIcons", "terminalsAndIcons.xml"),
                ("extra/org.fmi-standard.fmi-ls-bus", "fmi-ls-manifest.xml"),
            ):
                (root / directory).mkdir(parents=True)
                shutil.copy(descriptions / filename, root / directory)
            metadata_patch = PATCH.with_name("manifest.patch")
            subprocess.run(
                ["patch", "-p1", "-i", str(metadata_patch.resolve())],
                cwd=root,
                check=True,
            )
            md = ET.parse(root / "modelDescription.xml").getroot()
            variables = md.findall("ModelVariables/Binary")
            if not variables or any(
                v.get("mimeType") != PROFILE["mime_type"] for v in variables
            ):
                raise SystemExit(
                    f"{descriptions}: Binary variables must declare {PROFILE['mime_type']!r}"
                )
            library = root / "binaries/x86_64-linux/DemoCanNodeTriggeredOutput.so"
            library.parent.mkdir(parents=True)
            flags = [
                "-O2",
                "-fPIC",
                "-shared",
                "-DFMU_IDENTIFIER_H",
                "-Wl,--no-undefined",
            ]
            if receiver:
                flags.append("-DSIL_CAN_RECEIVE_ONLY")
            subprocess.run(
                [
                    "cc",
                    *flags,
                    "-I",
                    str(source),
                    *map(str, sorted(source.glob("*.c"))),
                    "-o",
                    str(library),
                ],
                check=True,
            )
            licenses = root / "documentation/licenses"
            licenses.mkdir(parents=True)
            shutil.copy(spec / "LICENSE.txt", licenses)
            shutil.copy(source / "fmi3PlatformTypes.h", licenses / "FMI-headers.txt")
            shutil.copy(PATCH, root / "documentation/node.patch")
            shutil.copy(metadata_patch, root / "documentation/manifest.patch")
            identity = {
                "examples_revision": EXAMPLES,
                "spec_revision": SPEC,
                "patch_sha256": digest(PATCH),
                "manifest_patch_sha256": digest(metadata_patch),
                "receive_only": receiver,
                "flags": flags,
                "fmpy": fmpy.__version__,
                "sources": {p.name: digest(p) for p in sorted(source.iterdir())},
            }
            if fault_aware:
                shutil.copy(FAULT_PATCH, root / "documentation/fault-aware-node.patch")
                identity.update(
                    fault_aware_bus_error=True,
                    fault_node_patch_sha256=digest(FAULT_PATCH),
                )
            (root / "resources").mkdir()
            (root / "resources/identity.json").write_text(
                json.dumps(identity, indent=2, sort_keys=True) + "\n"
            )
            suffix = "Receiver.fmu" if receiver else "Sender.fmu"
            target = output / (
                f"FaultAware{suffix}" if fault_aware else f"External{suffix}"
            )
            pack(root, target)
            print(f"{digest(target)}  {target.name}")


if __name__ == "__main__":
    build(*map(Path, sys.argv[1:]))
