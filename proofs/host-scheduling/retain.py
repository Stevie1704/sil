"""Keep a bounded, digest-bound complete Actions artifact for offline replay."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tarfile

from evidence import sha256


def retain(source: Path, output: Path, run_url: str):
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "portable-evidence.tar.xz"
    if archive.exists() or (output / "archive.sha256").exists():
        raise FileExistsError(archive)
    files = sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    if any(p.is_symlink() for p in files) or sum(p.stat().st_size for p in files) > 512 << 20:
        raise ValueError("artifact contains symlinks or exceeds the 512 MiB retention budget")
    identities = [json.loads(p.read_text()) for p in source.rglob("identity.json")]
    revisions = {i["source_revision"] for i in identities}
    if len(revisions) != 1 or None in revisions:
        raise ValueError("artifact has ambiguous measurement identity")
    import io
    metadata = (json.dumps({"run_url": run_url, "measurement_revision": revisions.pop(),
                           "files": len(files), "payload_bytes": sum(p.stat().st_size for p in files),
                           "omissions": ["Python bytecode caches only"]}, indent=2) + "\n").encode()
    with tarfile.open(archive, "w:xz") as tar:
        for path in files:
            tar.add(path, arcname=str(path.relative_to(source)), recursive=False)
        item = tarfile.TarInfo("retention.json")
        item.size = len(metadata)
        tar.addfile(item, io.BytesIO(metadata))
    digest = sha256(archive)
    (output / "archive.sha256").write_text(f"{digest}  {archive.name}\n")
    print(json.dumps({"archive_bytes": archive.stat().st_size, "payload_bytes": sum(p.stat().st_size for p in files)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    retain(args.artifact, args.output, args.run_url)
