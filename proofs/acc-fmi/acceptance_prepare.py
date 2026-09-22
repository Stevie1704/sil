"""Prepare the pinned acceptance bundle. Runs once, in the tool image.

This is the only step that needs the exporter and the independent importer.
It writes the model artifacts, their audits, the authored configurations and
the independent trajectories, then records a digest of every one of them.
Acceptance Runs consume that bundle and add nothing to it.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import acceptance_bundle
from acceptance_contract import (
    MODELS,
    REFERENCE_DIR,
    SCENARIO_CHECKS,
    SENSITIVITY_CHECKS,
    fmi_profile,
    handoff,
    reference_names,
    scenario_checks,
    sensitivity_row,
)
from proof_support import compare_files, file_sha256, require, run_logged, write_json
from qualify import capture_environment, inspect_archives
from scenarios import independent_trajectory
from sensitivity import validate_reference_initialization
from sensitivity_contract import configuration as sensitivity_configuration

HERE = Path(__file__).resolve().parent
GATE_TESTS = ("test_acceptance.py", "test_build.py")


def _archive_identity(path: Path) -> dict:
    """Everything a rebuild could silently change without changing behavior."""
    with zipfile.ZipFile(path) as archive:
        members = {
            info.filename: {"date_time": list(info.date_time), "crc": info.CRC}
            for info in archive.infolist()
        }
        root = ET.fromstring(archive.read("modelDescription.xml"))
    return {
        "sha256": file_sha256(path),
        "instantiation_token": root.attrib["instantiationToken"],
        "generation_date_present": "generationDateAndTime" in root.attrib,
        "members": members,
    }


def archive_reproducibility(out: Path, work: Path) -> dict:
    """Two controlled builds, compared as archives rather than as Runs.

    Run determinism is judged separately, from Recordings of the installed
    bundle. Here the question is only whether the exporter produces the same
    archive bytes, ZIP member timestamps and instantiation tokens twice.
    """
    builds = []
    for attempt in (1, 2):
        destination = work / f"build-{attempt}"
        run_logged([sys.executable, HERE / "build.py", destination],
                   out / f"archive-build-{attempt}.log")
        builds.append(destination)
    results = {}
    for model in MODELS:
        pinned = Path(f"/fmus/{model}.fmu")
        first, second = (build / f"{model}.fmu" for build in builds)
        identities = [_archive_identity(path) for path in (pinned, first, second)]
        require(
            identities[0] == identities[1] == identities[2],
            f"{model}: two controlled builds differ in archive identity",
        )
        require(
            not identities[0]["generation_date_present"],
            f"{model}: the archive kept a generation timestamp",
        )
        results[model] = {
            "build_sha256": compare_files(first, second),
            "pinned_sha256": identities[0]["sha256"],
            "instantiation_token": identities[0]["instantiation_token"],
            "member_date_times": sorted(
                {tuple(member["date_time"]) for member in identities[0]["members"].values()}
            ),
            "members": len(identities[0]["members"]),
        }
    return results


def scenario_references(out: Path, work: Path) -> dict:
    """Pin the independent trajectory and the configuration it was built from."""
    configurations = out / "configurations"
    configurations.mkdir(parents=True, exist_ok=True)
    pinned = {}
    for name, config in scenario_checks().items():
        config_path = configurations / f"scenario-{name}.json"
        write_json(config_path, config)
        # Retain the whole document: the open-loop qualification points the
        # independent driver checks before the scenario trajectory.
        independent_trajectory(config, config_path, work)
        reference = out / REFERENCE_DIR / f"scenario-{name}.json"
        shutil.copyfile(work / f"{name}.fmpy.json", reference)
        pinned[name] = {"file": str(reference.relative_to(out)),
                        "sha256": file_sha256(reference)}
    return pinned


def sensitivity_references(out: Path, work: Path) -> dict:
    configuration = sensitivity_configuration()
    config_path = out / "configurations" / "sensitivity.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(config_path, configuration)
    pinned = {}
    for name in reference_names():
        row = sensitivity_row(name)
        produced = work / f"{name}.fmpy.json"
        run_logged(
            [sys.executable, HERE / "sensitivity_reference.py", name, produced, config_path],
            work / f"{name}-reference.log",
        )
        document = json.loads(produced.read_text())
        validate_reference_initialization(document, configuration, row)
        reference = out / REFERENCE_DIR / f"sensitivity-{name}.json"
        shutil.copyfile(produced, reference)
        pinned[name] = {"file": str(reference.relative_to(out)),
                        "sha256": file_sha256(reference),
                        "row": row.to_document()}
    return pinned


def _version(packages: dict, name: str) -> str:
    """Distribution names are reported as declared, not as the lock spells them."""
    for installed, version in packages.items():
        if installed.lower().replace("-", "_") == name:
            return version
    raise RuntimeError(f"{name} is not installed in the tool image")


def prepare(out: Path, work: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / REFERENCE_DIR).mkdir(exist_ok=True)
    run_logged([sys.executable, "-m", "pytest", *[HERE / name for name in GATE_TESTS], "-q"],
               out / "gate-tests.log")
    capture_environment(out)
    environment = json.loads((out / "environment.json").read_text())

    fmus = out / "fmus"
    fmus.mkdir(exist_ok=True)
    inspect_archives(fmus)
    for name in ("archives.json", "schema-identity.json"):
        (fmus / name).replace(out / name)
    archives = json.loads((out / "archives.json").read_text())

    write_json(out / "archive-reproducibility.json", archive_reproducibility(out, work))
    write_json(out / "fmi-profile.json", fmi_profile(archives))
    write_json(out / "handoff.json", handoff())
    references = {
        "scenarios": scenario_references(out, work),
        "sensitivity": sensitivity_references(out, work),
    }
    write_json(out / "references.json", references)

    packages = environment["packages"]
    return acceptance_bundle.write_index(out, {
        "source_revision": environment["source_revision"],
        "machine_class": environment["machine"],
        "exporter": f"PythonFMU3 {_version(packages, 'pythonfmu3')}",
        "independent_importer": f"FMPy {_version(packages, 'fmpy')}",
        "fmu_sha256": {
            model: archives[model]["archive_sha256"] for model in MODELS
        },
        "scenario_checks": list(SCENARIO_CHECKS),
        "sensitivity_checks": list(SENSITIVITY_CHECKS),
        "pinned_reference_rows": sorted(references["sensitivity"]),
    })


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        prepare(Path(sys.argv[1]).resolve(), Path(temporary))
