"""Small artifact and command helpers for the disposable #330 experiments."""
from __future__ import annotations

import hashlib
import json
import subprocess


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def digest(path):
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"sha256": checksum, "bytes": path.stat().st_size}


def command(args, *, timeout=60):
    done = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                          timeout=timeout)
    return {"command": list(map(str, args)), "exit_code": done.returncode,
            "stdout": done.stdout, "stderr": done.stderr}
