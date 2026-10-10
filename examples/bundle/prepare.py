"""Prepare the three example regression bundles (issue #200).

Each bundle holds everything its Runs read: the authored Manifests, the
target, the participant code, the Recordings and the conversion inputs they
came from, the prepared references and the comparison contracts. Its
`bundle.json` names every file with its role and declares what the Runs need
outside the bundle: the runner, the interpreter, the Python modules the
adapters import, and the environment. It also names what must be absent: a
compiler, the FMI exporter and an independent importer.

- `library`: recorded speed input replayed into the `speed_filter` shared
  library through `examples/library/adapter.py`; its Test participant is
  the KPI.
- `fmu-replay`: recorded acceleration replayed into the `EgoMotion` FMU,
  compared with the closed-form reference.
- `coupling`: two coupled `Feedthrough` FMUs, and the same loop with `left`
  replaced by its Recording, each compared with the prepared coupled Run.

Preparation is the step that needs the source checkout, the compiler and a
runner; it writes the bundle where the runtime will find it, because the
Manifests name their artifacts by absolute path. `sil bundle seal` then runs
in the runtime, and `sil bundle run` executes the bundle there offline:

    python examples/bundle/prepare.py library /bundles/library \\
        --python-bin /opt/sil/venv/bin --sil-bin /opt/sil/bin \\
        --library build/example-speed_filter.so
    sil bundle seal /bundles/library
    sil bundle run /bundles/library -o evidence/library
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from sil.bundle import DECLARATION, FORMAT
from sil.csv_recording import convert
from sil.fmi.authoring import author
from sil.fmi.coupling import couple
from sil.fmi.substitution import substitute

EXAMPLES = Path(__file__).resolve().parents[1]
# The Python modules the participants import. `_ctypes` is the native
# extension the library adapter and the FMI Importer load targets through.
MODULES = ["sil", "mcap", "ctypes", "_ctypes"]
EXCLUDED = {
    "executables": ["cc", "gcc", "clang", "c++"],
    "modules": ["fmpy", "pythonfmu3"],
}
LIBRARY_TIMEOUT_MS = 10_000


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def native_libraries(binary: Path) -> list[str]:
    """The shared libraries the Linux loader resolves for `binary`.

    A provenance side-car digests the target a command names, never what the
    target loads, so these are declared as file dependencies. Other hosts
    declare none: the supported acceptance platform is Linux x86-64."""
    if not sys.platform.startswith("linux"):
        return []
    listing = subprocess.run(["ldd", str(binary)], check=True,
                             capture_output=True, text=True).stdout
    return sorted({word for line in listing.splitlines()
                   for word in line.split() if word.startswith("/")})


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Runtime(NamedTuple):
    """Where the installed runtime keeps its interpreter and its runner."""

    python_bin: Path
    sil_bin: Path

    def declaration(self, name: str, artifacts: dict[str, str], runs: list[dict],
                    files: tuple[str, ...] = ()) -> dict:
        return {
            "sil_bundle": FORMAT,
            "name": name,
            "artifacts": artifacts,
            "runtime": {
                "runner": "sil-run",
                # The whole environment of every Run; nothing else is inherited.
                "environment": {
                    "PATH": f"{self.python_bin}:{self.sil_bin}",
                    "PYTHONNOUSERSITE": "1",
                },
            },
            "dependencies": {
                "executables": ["python3"],
                "python": {"interpreter": "python3", "modules": MODULES},
                "files": list(files),
            },
            "excluded": EXCLUDED,
            "runs": runs,
        }


class Bundle:
    """A bundle directory being prepared, and the role of each file in it."""

    def __init__(self, root: Path):
        if root.exists() and any(root.iterdir()):
            raise SystemExit(f"{root} is not empty; name a new directory")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.artifacts: dict[str, str] = {}

    def path(self, name: str, role: str) -> Path:
        self.artifacts[name] = role
        return self.root / name

    def copy(self, source: Path, role: str, name: str | None = None) -> Path:
        target = self.path(name or source.name, role)
        shutil.copyfile(source, target)
        return target

    def converted(self, mapping: Path, csv: Path, role: str, name: str) -> Path:
        """The conversion inputs, the Recording and its receipt."""
        mapping = self.copy(mapping, "conversion-input")
        csv = self.copy(csv, "conversion-input")
        recording = self.path(f"{name}.mcap", role)
        receipt = convert(mapping, csv, recording)
        _write_json(self.path(f"{name}.receipt.json", "receipt"), receipt)
        return recording

    def declare(self, runtime: Runtime, name: str, runs: list[dict],
                files: tuple[str, ...] = ()) -> Path:
        path = self.root / DECLARATION
        _write_json(path, runtime.declaration(name, self.artifacts, runs, files))
        return path


def library(root: Path, runtime: Runtime, library_binary: Path, *,
            bundled: bool = True) -> Path:
    """The shared-library bundle. With `bundled` false the library stays
    installed where it is, outside the bundle, and is declared as a file
    dependency instead, as a vendor library installed on the runtime is."""
    example = EXAMPLES / "library"
    manifest = _load("library_manifest", example / "manifest.py")
    bundle = Bundle(root)
    for name in ("adapter.py", "binding.py", "filter_test.py"):
        bundle.copy(example / name, "participant")
    recording = bundle.converted(example / "mapping.json", example / "signals.csv",
                                 "recording", "signals")
    target = (bundle.copy(library_binary, "target", "speed_filter.so")
              if bundled else library_binary.resolve())
    manifest.library_manifest(recording, target, participants=bundle.root).write(
        bundle.path("library.json", "manifest"))
    runs = [{"name": "library", "manifest": "library.json",
             "participant_timeout_ms": LIBRARY_TIMEOUT_MS}]
    installed = () if bundled else (str(target),)
    return bundle.declare(runtime, "library", runs,
                          files=(*installed, *native_libraries(target)))


def fmu_replay(root: Path, runtime: Runtime, fmu_binary: Path, *,
               reference_csv: Path | None = None) -> Path:
    """The single-FMU bundle. `reference_csv` replaces the closed-form
    reference, which is how a reference mismatch is demonstrated."""
    example = EXAMPLES / "fmu-replay"
    packaging = _load("fmu_replay_package", example / "package.py")
    bundle = Bundle(root)
    fmu = packaging.package(fmu_binary, bundle.path("EgoMotion.fmu", "target"))
    recording = bundle.converted(example / "mapping.json", example / "recorded.csv",
                                 "recording", "recorded")
    bundle.converted(example / "reference-mapping.json",
                     reference_csv or example / "reference.csv", "reference", "reference")
    bundle.copy(example / "contract.json", "contract")
    authoring = bundle.copy(example / "authoring.json", "resource")
    receipt = author(authoring, fmu, recording, bundle.path("fmu-replay.json", "manifest"))
    _write_json(bundle.path("fmu-replay.receipt.json", "receipt"), receipt)
    runs = [{"name": "fmu-replay", "manifest": "fmu-replay.json",
             "comparisons": [{"name": "reference", "contract": "contract.json",
                              "reference": "reference.mcap"}]}]
    return bundle.declare(runtime, "fmu-replay", runs,
                          files=tuple(native_libraries(fmu_binary)))


def coupling(root: Path, runtime: Runtime, fmu: Path, runner: Path) -> Path:
    """The coupled-FMU bundle. `runner` records the prepared reference: the
    coupled Run's Recording, which is also what replaces `left`."""
    example = EXAMPLES / "fmu-coupling"
    bundle = Bundle(root)
    archive = bundle.copy(fmu, "target", "Feedthrough.fmu")
    fmus = {"left": archive, "right": archive}
    document = bundle.copy(example / "feedback.json", "resource")
    coupled = bundle.path("coupled.json", "manifest")
    couple(document, fmus, coupled)
    reference = bundle.path("coupled.mcap", "reference")
    _record(runner, coupled, reference)
    substitute(document, fmus, "left", reference,
               bundle.path("substituted.json", "manifest"),
               bundle.path("retained.contract.json", "contract"))
    compared = [{"name": "retained", "contract": "retained.contract.json",
                 "reference": "coupled.mcap"}]
    runs = [{"name": "coupled", "manifest": "coupled.json", "comparisons": compared},
            {"name": "substituted", "manifest": "substituted.json",
             "comparisons": compared}]
    return bundle.declare(runtime, "coupling", runs)


def _record(runner: Path, manifest: Path, recording: Path) -> None:
    """One preparation Run, outside the bundle so its side-car stays out."""
    with tempfile.TemporaryDirectory() as work:
        produced = Path(work) / "run.mcap"
        subprocess.run([str(runner), str(manifest), "-o", str(produced)],
                       cwd=work, check=True, capture_output=True, text=True)
        shutil.copyfile(produced, recording)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=["library", "fmu-replay", "coupling"])
    parser.add_argument("bundle", type=Path, help="a new directory")
    parser.add_argument("--python-bin", type=Path, required=True,
                        help="the runtime directory holding python3 with SiL installed")
    parser.add_argument("--sil-bin", type=Path, required=True,
                        help="the runtime directory holding sil-run")
    parser.add_argument("--library", type=Path, help="the speed_filter build")
    parser.add_argument("--fmu-binary", type=Path, help="the EgoMotion build")
    parser.add_argument("--fmu", type=Path, help="the Feedthrough archive")
    parser.add_argument("--runner", type=Path,
                        help="the runner that records the coupled reference")
    args = parser.parse_args()
    needed = {"library": "library", "fmu-replay": "fmu_binary", "coupling": "fmu"}[args.kind]
    if getattr(args, needed) is None or (args.kind == "coupling" and args.runner is None):
        parser.error(f"{args.kind} needs --{needed.replace('_', '-')}"
                     + (" and --runner" if args.kind == "coupling" else ""))
    runtime = Runtime(args.python_bin.resolve(), args.sil_bin.resolve())
    if args.kind == "library":
        library(args.bundle, runtime, args.library)
    elif args.kind == "fmu-replay":
        fmu_replay(args.bundle, runtime, args.fmu_binary)
    else:
        coupling(args.bundle, runtime, args.fmu, args.runner)


if __name__ == "__main__":
    main()
