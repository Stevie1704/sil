"""Dependency-free evidence assertions, command execution and byte identities."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

# A wall-clock response deadline, never part of the authored exchange grid.
PARTICIPANT_TIMEOUT_MS = int(os.environ.get("SIL_ACC_PARTICIPANT_TIMEOUT_MS", "30000"))


def require(condition, diagnostic):
    if not condition:
        raise RuntimeError(diagnostic)


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def run_expecting(args, log, code):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            timeout=max(120, PARTICIPANT_TIMEOUT_MS / 1000 * 4))
    output = result.stdout + result.stderr
    log.write_text(output)
    require(result.returncode == code,
            f"exit {result.returncode}, expected {code}: {args}; see {log}")
    return output


def run_logged(args, log):
    return run_expecting(args, log, 0)


def sil_runner_args(manifest, recording):
    return [
        "/build/sil-run", str(manifest), "-o", str(recording),
        "--participant-timeout-ms", str(PARTICIPANT_TIMEOUT_MS),
    ]


def run_manifest_twice(factory, name, out: Path):
    """Author one Manifest twice and execute that exact Manifest twice."""
    path = out / f"{name}.json"
    manifest_hash = factory().write(path).hash
    authored_again = out / f"{name}-authored-again.json"
    factory().write(authored_again)
    authored_hashes = compare_files(path, authored_again)
    recordings = [out / f"{name}-{repeat}.mcap" for repeat in (1, 2)]
    for recording in recordings:
        run_logged(sil_runner_args(path, recording), recording.with_suffix(".log"))
    return {
        "manifest_sha256": manifest_hash,
        "authored_manifest_sha256": authored_hashes,
        "recording_sha256": compare_files(*recordings),
        "recording_files": [path.name for path in recordings],
        "kpi_log": recordings[0].with_suffix(".log").name,
    }


def compare_files(first, second):
    """Retain both identities, and require actual byte equality."""
    hashes = [file_sha256(first), file_sha256(second)]
    require(first.read_bytes() == second.read_bytes(),
            f"byte mismatch: {first} ({hashes[0]}) != {second} ({hashes[1]})")
    return hashes
