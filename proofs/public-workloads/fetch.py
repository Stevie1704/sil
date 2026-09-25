"""The one networked step: fetch every pinned public source and verify it.

Files must match their pinned SHA-256 and size; the Git source must resolve to
its pinned commit and tree. A changed upstream artifact fails here, naming the
source, instead of silently changing a later comparison.
"""
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = HERE / "sources.json"


def verify(name, data, pin):
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != pin["size"] or digest != pin["sha256"]:
        raise RuntimeError(
            f"{name}: fetched {len(data)} bytes with sha256 {digest}; "
            f"pinned {pin['size']} bytes with sha256 {pin['sha256']}")


def fetch_file(name, source, destination):
    with urllib.request.urlopen(source["url"], timeout=120) as response:
        data = response.read()
    verify(name, data, source)
    (destination / name).write_bytes(data)


def fetch_git(name, source, destination):
    checkout = destination / name
    git = ["git", "-C", str(checkout)]
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run([*git, "fetch", "-q", "--depth", "1", source["repository"],
                    source["commit"]], check=True)
    subprocess.run([*git, "checkout", "-q", "--detach", source["commit"]], check=True)
    for ref, pinned in (("HEAD", source["commit"]), ("HEAD^{tree}", source["tree"])):
        resolved = subprocess.run([*git, "rev-parse", ref], check=True,
                                  capture_output=True, text=True).stdout.strip()
        if resolved != pinned:
            raise RuntimeError(f"{name}: {ref} is {resolved}; pinned {pinned}")


def fetch(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in json.loads(SOURCES.read_text()).items():
        (fetch_git if source["kind"] == "git" else fetch_file)(name, source, destination)


if __name__ == "__main__":
    fetch(Path(sys.argv[1]))
