"""Build with the pinned exporter; normalize only archive identity metadata."""
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pythonfmu3.builder import FmuBuilder

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for model in ("controller", "plant"):
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            source = HERE / "models" / f"{model}.py"
            dynamics = ROOT / "python/src/sil/examples/acc/dynamics.py"
            identity = {
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
            (staging / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
            shutil.copy(source, staging)
            shutil.copy(dynamics, staging)
            # Licenses travel with every FMU, alongside the source.
            shutil.copy(ROOT / "LICENSE", staging / "LICENSE-SiL")
            shutil.copy(HERE / "pythonfmu3-LICENSE", staging / "LICENSE-PythonFMU3")
            raw = FmuBuilder.build_FMU(
                staging / source.name, dest=staging / "raw",
                project_files=[staging / name for name in (
                    "dynamics.py", "identity.json", "LICENSE-SiL", "LICENSE-PythonFMU3")],
                needsExecutionTool=True, canHandleVariableCommunicationStepSize=False,
            )
            with zipfile.ZipFile(raw) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            xml = ET.fromstring(members["modelDescription.xml"])
            # PythonFMU3 generates UUID1 + wall-clock date and ZIP mtimes.
            # Neither participates in the model equations. Bind the token to
            # all shipped sources/options instead; never patch FMI behavior.
            token = sha256(json.dumps(identity, sort_keys=True).encode())
            xml.set("instantiationToken", token)
            xml.attrib.pop("generationDateAndTime")
            members["modelDescription.xml"] = ET.tostring(xml, encoding="utf-8", xml_declaration=True)
            target = destination / raw.name
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, data in sorted(members.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
            identity["archive_sha256"] = sha256(target.read_bytes())
            (destination / f"{target.stem}.identity.json").write_text(json.dumps(identity, indent=2) + "\n")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
