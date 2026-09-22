"""Dependency-free evidence assertions, command execution and byte identities."""
import hashlib
import json
import os
import subprocess

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


def compare_files(first, second):
    """Retain both identities, and require actual byte equality."""
    hashes = [file_sha256(first), file_sha256(second)]
    require(first.read_bytes() == second.read_bytes(),
            f"byte mismatch: {first} ({hashes[0]}) != {second} ({hashes[1]})")
    return hashes
