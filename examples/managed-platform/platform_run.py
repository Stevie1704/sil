#!/usr/bin/env python3
"""One Run under `--provenance-mode external`, as a managed platform does it.

    python platform_run.py --execution-id exec-0001 --workdir NEW_DIRECTORY

The platform owns artifact identity, so this script does these steps:

1. Stage the inputs in a read-only Run directory, and take their identities.
2. Hash the exact Manifest bytes that SiL will read.
3. Run SiL in external mode. SiL omits its provenance-only digests.
4. Check the side-car: the Manifest digest must be equal, and each resolved
   path must have an identity. An omitted digest is not a verification.
5. Hash the finalized Recording.
6. Write `execution-record.json`, which associates all of this with the
   identifier of the external execution.

No SiL interface accepts these identities, and SiL does not verify them. A real
platform takes each input identity from its artifact store. It does not hash
the inputs for each Run, because the total work would then not decrease.

The exit code is that of the Run, or 3 if the Run read other inputs than the
platform bound.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from sil.manifest import Manifest

HERE = Path(__file__).resolve().parent
SCHEMAS = {"example.Count": {"fields": [{"name": "value", "type": "u64"}]}}
UNBOUND_INPUT = 3


def file_digest(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def stage_inputs(run_directory: Path) -> Path:
    """Make the read-only Run directory and give the path of its Manifest."""
    run_directory.mkdir(parents=True)
    participant = run_directory / "participant.py"
    shutil.copyfile(HERE / "participant.py", participant)
    manifest = Manifest(duration_ns=100_000_000)
    manifest.add_schemas(SCHEMAS)
    manifest.add_channel("counts", schema="example.Count")
    manifest.add_process("counter", command=[sys.executable, "participant.py"],
                         step_period_ns=10_000_000, publishes=["counts"])
    path = manifest.write(run_directory / "manifest.json").path
    for file in (participant, path):
        file.chmod(0o444)
    run_directory.chmod(0o555)
    return path


def take_identities(run_directory: Path) -> dict[str, str]:
    """Canonical path to digest, for the runner and each input."""
    runner = shutil.which("sil-run")
    if runner is None:
        raise SystemExit("cannot find sil-run on PATH")
    inputs = [runner, sys.executable, run_directory / "participant.py"]
    return {os.path.realpath(path): file_digest(Path(path)) for path in inputs}


def resolved_paths(side_car: dict) -> dict[str, str]:
    """Each artifact path of the side-car, with its role."""
    artifacts = side_car["artifacts"]
    paths = {artifacts["runner"]["path"]: "runner"}
    if artifacts["clock_shim"] is not None:
        paths[artifacts["clock_shim"]["path"]] = "clock_shim"
    for entry in artifacts["native_participants"]:
        paths[entry["library"]["path"]] = "library"
    for entry in artifacts["process_participants"]:
        paths[entry["executable"]["path"]] = "executable"
    for entry in artifacts["command_files"]:
        paths[entry["file"]["path"]] = "command_file"
    return paths


def run_sil(manifest: Path, recording: Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "sil.cli", "run", str(manifest),
         "-o", str(recording), "--provenance-mode", "external"]).returncode


def execution_record(execution_id: str, manifest: Path, manifest_digest: str,
                     identities: dict[str, str], side_car: dict,
                     recording: Path, exit_code: int) -> dict:
    roles = resolved_paths(side_car)
    return {
        "execution_id": execution_id,
        "run_exit_code": exit_code,
        "provenance_mode": side_car["provenance_mode"],
        "manifest": {"path": str(manifest), "sha256": manifest_digest},
        "inputs": [{"role": roles[path], "path": path,
                    "platform_sha256": identities[path]}
                   for path in sorted(roles)],
        "output": {"path": str(recording), "bytes": recording.stat().st_size,
                   "platform_sha256": file_digest(recording)},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execution-id", required=True,
                        help="the identifier of the external execution record")
    parser.add_argument("--workdir", type=Path, required=True,
                        help="a directory that does not exist yet")
    args = parser.parse_args(argv)
    workdir = args.workdir.resolve()
    if workdir.exists():
        parser.error(f"{workdir} exists; each execution needs a new directory")

    manifest = stage_inputs(workdir / "inputs")
    identities = take_identities(workdir / "inputs")
    manifest_digest = file_digest(manifest)

    outputs = workdir / "outputs"
    outputs.mkdir()
    recording = outputs / "out.mcap"
    exit_code = run_sil(manifest, recording)

    side_car = json.loads(
        recording.with_name("out.mcap.provenance.json").read_text())
    if side_car["manifest_hash"] != manifest_digest:
        print("the Run did not read the staged Manifest", file=sys.stderr)
        return UNBOUND_INPUT
    unbound = sorted(set(resolved_paths(side_car)) - set(identities))
    if unbound:
        print(f"no identity for: {', '.join(unbound)}", file=sys.stderr)
        return UNBOUND_INPUT

    record = execution_record(args.execution_id, manifest, manifest_digest,
                              identities, side_car, recording, exit_code)
    (workdir / "execution-record.json").write_text(
        json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
