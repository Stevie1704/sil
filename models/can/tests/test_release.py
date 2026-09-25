"""Release packaging: identity, capability declaration, linkage and rebuild."""

import hashlib
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "build/can"
PROFILE = json.loads((ROOT / "models/can/profile.json").read_text())
MODEL = PROFILE["model_name"]
VERSION = PROFILE["version"]
RELEASE = ARTIFACTS / "release"
RELEASED_FMU = RELEASE / f"{MODEL}-{VERSION}-x86_64-linux.fmu"
# The C and C++ runtime of the supported platform; nothing from SiL or Python.
ALLOWED_LIBRARIES = {"libstdc++.so.6", "libm.so.6", "libgcc_s.so.1", "libc.so.6"}


def model_description():
    with zipfile.ZipFile(ARTIFACTS / f"{MODEL}.fmu") as archive:
        return ET.fromstring(archive.read("modelDescription.xml"))


def test_model_description_names_the_release():
    description = model_description()
    assert description.get("version") == VERSION
    assert description.get("license") == "Apache-2.0"
    assert "Classical CAN" in description.get("description")


def test_declared_capabilities_are_only_the_supported_ones():
    description = model_description()
    assert description.find("ModelExchange") is None
    assert description.find("ScheduledExecution") is None
    cosimulation = description.find("CoSimulation")
    assert {k: v for k, v in cosimulation.attrib.items() if v == "true"} == {
        "hasEventMode": "true",
        "canHandleVariableCommunicationStepSize": "true",
    }


def test_archive_ships_release_notes_and_licenses():
    with zipfile.ZipFile(ARTIFACTS / f"{MODEL}.fmu") as archive:
        names = set(archive.namelist())
        assert archive.read("documentation/RELEASE.md") == (
            ROOT / "models/can/RELEASE.md"
        ).read_bytes()
    assert {
        "documentation/README.md",
        "documentation/licenses/LICENSE",
        "documentation/licenses/NOTICE",
        "documentation/licenses/FMI-BSD-2-Clause.txt",
    } <= names


def test_library_links_only_the_platform_runtime(tmp_path):
    with zipfile.ZipFile(ARTIFACTS / f"{MODEL}.fmu") as archive:
        archive.extractall(tmp_path)
    library = tmp_path / f"binaries/x86_64-linux/{MODEL}.so"
    dynamic = subprocess.check_output(["readelf", "-d", str(library)], text=True)
    needed = {
        line.split("[", 1)[1].rstrip("]")
        for line in dynamic.splitlines() if "(NEEDED)" in line
    }
    assert needed <= ALLOWED_LIBRARIES


def test_packaged_sources_rebuild_the_same_library_without_python(tmp_path):
    with zipfile.ZipFile(ARTIFACTS / f"{MODEL}.fmu") as archive:
        archive.extractall(tmp_path)
    library = tmp_path / f"binaries/x86_64-linux/{MODEL}.so"
    shipped = library.read_bytes()
    library.unlink()
    subprocess.run(["sh", "sources/build.sh"], cwd=tmp_path, check=True,
                   env={"PATH": "/usr/bin:/bin"}, timeout=300)
    assert library.read_bytes() == shipped


def test_release_directory_publishes_digests_and_identities():
    release = json.loads((RELEASE / "release.json").read_text())
    with zipfile.ZipFile(ARTIFACTS / f"{MODEL}.fmu") as archive:
        identity = json.loads(archive.read("resources/identity.json"))
    assert RELEASED_FMU.read_bytes() == (ARTIFACTS / f"{MODEL}.fmu").read_bytes()
    digest = hashlib.sha256(RELEASED_FMU.read_bytes()).hexdigest()
    assert release["fmu"] == {"file": RELEASED_FMU.name, "sha256": digest}
    assert release["version"] == VERSION
    assert release["platform"] == "x86_64-linux"
    assert release["reproducibility"] == {
        "controlled_builds": 2, "byte_identical": True,
    }
    assert release["build"]["git_revision"] == identity["git_revision"]
    assert release["build"]["compiler"] == identity["compiler"]
    assert release["build"]["flags"] == identity["flags"]
    assert release["build"]["fmi_headers"] == f"FMPy {identity['fmpy']}"
    assert release["build"]["image"] == (ARTIFACTS / "image.txt").read_text().strip()
    assert release["sources"] == identity["sources"]
    assert release["releasable"] is not identity["source_dirty"]
    assert (RELEASE / "SHA256SUMS").read_text() == (
        f"{digest}  {RELEASED_FMU.name}\n"
        f"{hashlib.sha256((RELEASE / 'release.json').read_bytes()).hexdigest()}"
        "  release.json\n"
    )


def test_release_refuses_differing_builds(tmp_path):
    first, second = tmp_path / "a.fmu", tmp_path / "b.fmu"
    shutil.copy(ARTIFACTS / f"{MODEL}.fmu", first)
    shutil.copy(ARTIFACTS / f"{MODEL}.fmu", second)
    with zipfile.ZipFile(second, "a") as archive:
        archive.writestr("documentation/extra.txt", "variability")
    result = subprocess.run(
        [sys.executable, str(ROOT / "models/can/release.py"), str(first),
         str(second), str(tmp_path / "out"), str(ARTIFACTS / "image.txt")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "differ" in result.stderr
    assert not (tmp_path / "out").exists()

