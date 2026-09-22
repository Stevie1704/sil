"""Build with the pinned exporter; normalize only archive identity metadata."""
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.parsers import expat

from pythonfmu3.builder import FmuBuilder

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def model_identity(source, dynamics):
    return {
        "source_revision": (ROOT / "source-revision.txt").read_text().strip(),
        "model_sha256": sha256(source.read_bytes()),
        "dynamics_sha256": sha256(dynamics.read_bytes()),
        "exporter": "PythonFMU3 0.3.4",
        "exporter_revision": "6c4b81cb869447f6c69e3417c495f73f11c40ae0",
        "python": "CPython 3.13.7 (provided by execution image)",
        "machine_class": "Linux x86-64, glibc 2.36",
        "model_license": "Apache-2.0",
        "exporter_license": "MIT",
    }


def stage_model(staging, source, dynamics, identity):
    (staging / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    shutil.copy(source, staging)
    shutil.copy(dynamics, staging)
    shutil.copy(ROOT / "LICENSE", staging / "LICENSE-SiL")
    shutil.copy(HERE / "pythonfmu3-LICENSE", staging / "LICENSE-PythonFMU3")


def normalize_xml_metadata(xml, token):
    """Splice just two root attributes; preserve every other original byte.

    Expat locates the real root, past comments/PIs, and validates the complete
    document. The attribute lexer only examines that start tag, preserving
    namespace prefixes, comments, formatting, order and all child elements.
    """
    parser = expat.ParserCreate()
    root = []

    def start(name, attributes):
        if not root:
            root.extend([parser.CurrentByteIndex, name])

    parser.StartElementHandler = start
    parser.Parse(xml, True)
    offset, name = root
    if name.split(":")[-1] != "fmiModelDescription":
        raise ValueError("Expected an FMI model description root")
    # XML attribute quotes cannot occur literally inside their own value.
    tag = re.match(rb"<[^\s/>]+(?:[^\"'>]|\"[^\"]*\"|'[^']*')*>", xml[offset:])
    if tag is None:
        raise ValueError("Cannot locate the root start tag")
    attributes = re.finditer(
        rb"\s+([^\s=/>]+)\s*=\s*(['\"])(.*?)\2", tag.group(), re.DOTALL)
    edits = []
    found_token = False
    for attribute in attributes:
        if attribute[1] == b"instantiationToken":
            edits.append((attribute.start(3), attribute.end(3), token.encode("ascii")))
            found_token = True
        elif attribute[1] == b"generationDateAndTime":
            edits.append((attribute.start(), attribute.end(), b""))
    if not found_token:
        raise ValueError("Missing instantiationToken")
    for begin, end, replacement in reversed(edits):
        xml = xml[:offset + begin] + replacement + xml[offset + end:]
    return xml


def normalize_archive(raw, target, identity):
    with zipfile.ZipFile(raw) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    token = sha256(json.dumps(identity, sort_keys=True).encode())
    members["modelDescription.xml"] = normalize_xml_metadata(members["modelDescription.xml"], token)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def build(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for model in ("controller", "plant"):
        source = HERE / "models" / f"{model}.py"
        dynamics = ROOT / "python/src/sil/examples/acc/dynamics.py"
        identity = model_identity(source, dynamics)
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            stage_model(staging, source, dynamics, identity)
            raw = FmuBuilder.build_FMU(
                staging / source.name, dest=staging / "raw",
                project_files=[staging / name for name in (
                    "dynamics.py", "identity.json", "LICENSE-SiL", "LICENSE-PythonFMU3")],
                needsExecutionTool=True, canHandleVariableCommunicationStepSize=False,
            )
            target = destination / raw.name
            normalize_archive(raw, target, identity)
        identity["archive_sha256"] = sha256(target.read_bytes())
        (destination / f"{target.stem}.identity.json").write_text(json.dumps(identity, indent=2) + "\n")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
