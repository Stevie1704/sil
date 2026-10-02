"""The one networked step: fetch the pinned OSMP checkout and verify it.

The superproject must resolve to its pinned commit and tree, and each
submodule to its pinned commit. The tree already fixes the submodule commits;
checking them again names the submodule if a fetch went wrong.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = HERE / "sources.json"


def resolve(checkout, ref):
    return subprocess.run(["git", "-C", str(checkout), "rev-parse", ref], check=True,
                          capture_output=True, text=True).stdout.strip()


def fetch_git(name, source, destination):
    checkout = destination / name
    git = ["git", "-C", str(checkout)]
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run([*git, "fetch", "-q", "--depth", "1", source["repository"],
                    source["commit"]], check=True)
    subprocess.run([*git, "checkout", "-q", "--detach", source["commit"]], check=True)
    subprocess.run([*git, "submodule", "update", "-q", "--init", "--recursive", "--depth", "1"],
                   check=True)
    pins = {".": source["commit"], **source["submodules"]}
    resolved = {path: resolve(checkout / path, "HEAD") for path in pins}
    resolved[".^{tree}"] = resolve(checkout, "HEAD^{tree}")
    pins[".^{tree}"] = source["tree"]
    if resolved != pins:
        raise RuntimeError(f"{name}: resolved {resolved}; pinned {pins}")


def fetch(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in json.loads(SOURCES.read_text()).items():
        fetch_git(name, source, destination)


if __name__ == "__main__":
    fetch(Path(sys.argv[1]))
