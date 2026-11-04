#!/usr/bin/env python3
"""Keep replay inputs and representative Recordings in a compact proof archive."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCES = ("qualify.py", "session.py", "model.py", "analyze.py", "validate.py",
           "process.py", "notify.hpp", "workload.c", "overlay.cmake")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def keep_recording(directory):
    return directory.endswith(("ordinary-0", "ordinary-failure-0")) or "-unclosed-" in directory or "-exhaustion-" in directory


def retained_bytes(path):
    """Drop duplicates; retain every input consumed by the offline validator."""
    if path.suffix == ".data" or path.name in ("analysis.json", "digests.json", "samples.txt"):
        return None
    data = path.read_bytes()
    if path.name == "run.mcap":
        result = json.loads((path.parent / "result.json").read_text())
        if sha256(data) != result["recording_sha256"]:
            raise ValueError(f"Recording digest mismatch: {path}")
    if path.name in ("run.mcap", "run.mcap.provenance.json", "run.mcap.failure.json"):
        if not keep_recording(path.parent.name):
            return None
    if path.name in ("metadata.txt", "symbols.txt"):
        result = json.loads((path.parent / "result.json").read_text())
        if result["exports"][path.name] == 0:
            token = b"PERF_RECORD" if path.name == "metadata.txt" else b"late_hot_function"
            data = b"".join(line for line in data.splitlines(keepends=True)
                            if token in line or b"warning" in line.lower() or b"error" in line.lower())
    return data


def collect(artifact):
    files, transformed, omitted = {}, {}, {}
    for path in sorted(artifact.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(artifact).as_posix()
        data = retained_bytes(path)
        if data is None:
            omitted[path.name] = omitted.get(path.name, 0) + 1
            continue
        original_digest = sha256(path.read_bytes())
        if original_digest != sha256(data):
            transformed[name] = {"original_sha256": original_digest, "retained_sha256": sha256(data)}
        files[name] = data
    return files, transformed, omitted


def proof_sources(artifact, files):
    revisions = {json.loads(path.read_text())["revision"]
                 for path in (artifact / "window-evidence").glob("*/identities.json")}
    revisions.discard(None)  # An unavailable default-capability container has no revision env.
    if len(revisions) != 1:
        raise ValueError("evidence does not identify one measured revision")
    revision = revisions.pop()
    for name in SOURCES:
        files[f"proof-sources/{name}"] = subprocess.check_output(
            ["git", "show", f"{revision}:proofs/capture-windows/{name}"], cwd=ROOT)
    files["proof-sources/retain.py"] = Path(__file__).read_bytes()
    return revision


def retain(artifact, output, run_url):
    output.mkdir(parents=True, exist_ok=False)
    files, transformed, omitted = collect(artifact)
    revision = proof_sources(artifact, files)
    manifest = {"measurement_revision": revision, "actions_run": run_url,
                "omitted_file_counts": omitted, "filtered_export_digests": transformed,
                "retention": "all validator inputs; metadata records; hot-function leaves; baseline and exceptional Recordings"}
    files["retention.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    archive = output / "portable-evidence.tar.xz"
    with tarfile.open(archive, mode="w:xz", preset=9) as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    (output / "archive.sha256").write_text(sha256(archive.read_bytes()) + "  " + archive.name + "\n")
    print(json.dumps({"files": len(files), "payload_bytes": sum(map(len, files.values())),
                      "archive_bytes": archive.stat().st_size, "measurement_revision": revision}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    retain(args.artifact.resolve(), args.output.resolve(), args.run_url)
