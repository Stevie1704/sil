"""The profiling proof's workload: its binaries and its Manifests (#314).

`build` compiles the workload with one compiler and `-O2 -g`, the
optimization of a measured build. On Linux it also makes the debug-file
variants: the Native library is stripped and gets a separate matching debug
file, and a second build with other code gives a mismatched debug file under
the same name.

`manifest` authors the Run: a Native participant, a shimmed Process
participant that loads a shared library and starts a short-lived child, and
the FMI Importer over an FMU that it extracts into the Run working directory.
A launch prefix can wrap Process participant commands; the proof uses it for
the Heaptrack launch adapter. Only those command entries differ.
"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

PROOF_DIR = Path(__file__).resolve().parent
SOURCES = PROOF_DIR / "workload"
ROOT = PROOF_DIR.parents[1]
INCLUDE = ROOT / "include"
EGO_MOTION = ROOT / "examples" / "fmu-replay"

MS = 1_000_000
PERIOD_NS = 10 * MS
DURATION_NS = 1000 * MS
ACTIVATIONS = DURATION_NS // PERIOD_NS
CFLAGS = ["-O2", "-g"]

SCHEMAS = {
    "proof.Native": {"fields": [{"name": "result", "type": "u64"},
                                {"name": "activation", "type": "u64"}]},
    "proof.Process": {"fields": [{"name": "result", "type": "u64"},
                                 {"name": "clock_now_ns", "type": "u64"},
                                 {"name": "child_clock_ns", "type": "u64"},
                                 {"name": "child_result", "type": "u64"}]},
    "fmu.In": {"fields": [{"name": "acceleration", "type": "f64"}]},
    "fmu.Out": {"fields": [{"name": "speed", "type": "f64"},
                           {"name": "position", "type": "f64"}]},
}

# Known hot functions and allocation sites, with the counts the C sources
# fix. A Process Step and a Native activation both happen ACTIVATIONS times.
CHILD_EVERY = 25
HOT_FUNCTIONS = {
    "native": ("silproof_native_spin_even", "silproof_native_spin_odd"),
    "process": ("silproof_process_spin",),
    "child": ("silproof_child_spin",),
    "fmu": ("silproof_fmu_spin",),
}
ALLOCATION_SITES = {
    # site: (calls, bytes per call)
    "silproof_native_alloc_site": (16 * ACTIVATIONS, 1024),
    "silproof_native_retain_site": (1, 65536),
    "silproof_process_alloc_site": (8 * ACTIVATIONS, 2048),
    "silproof_fmu_alloc_site": (4 * ACTIVATIONS, 4096),
}
CHILDREN = len(range(0, ACTIVATIONS, CHILD_EVERY))


@dataclass(frozen=True)
class Artifacts:
    native: Path  # the library the Manifest loads (stripped on Linux)
    native_full: Path  # the same build before strip
    native_debug: Path | None  # its matching separate debug file
    native_mismatched_debug: Path | None  # another build's debug file
    process: Path
    child: Path
    fmu: Path  # the archive
    fmu_binary: Path  # the library inside the archive
    adapter: Path


def _cc(out: Path, *sources: Path, shared: bool, extra=()) -> Path:
    command = [shutil.which("cc") or "cc", *CFLAGS, f"-I{INCLUDE}",
               f"-I{SOURCES}", f"-I{EGO_MOTION}", *extra]
    if shared:
        command += ["-shared", "-fPIC"]
    subprocess.run([*command, "-o", str(out), *map(str, sources)],
                   check=True)
    return out


def _split_debug(full: Path, stripped: Path, debug: Path) -> None:
    subprocess.run(["objcopy", "--only-keep-debug", str(full), str(debug)],
                   check=True)
    subprocess.run(["objcopy", "--strip-all", str(full), str(stripped)],
                   check=True)
    # The link names the file only; a reader finds it beside the library.
    subprocess.run(["objcopy", f"--add-gnu-debuglink={debug}",
                    str(stripped)], check=True)


def _package_fmu(binary: Path, out: Path) -> Path:
    spec = importlib.util.spec_from_file_location(
        "silproof_fmu_package", EGO_MOTION / "package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.package(binary, out)


def build(out: Path) -> Artifacts:
    """Compile every workload binary into `out` and package the FMU."""
    lib, bin_, debug = out / "lib", out / "bin", out / "debug"
    for d in (lib, bin_, debug / "matching", debug / "mismatched",
              out / "full"):
        d.mkdir(parents=True, exist_ok=True)
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    name = f"libsilproof_native{suffix}"
    native_full = _cc(out / "full" / name, SOURCES / "silproof_native.c",
                      shared=True)
    native = lib / name
    native_debug = mismatched_debug = None
    if platform.system() == "Linux":
        native_debug = debug / "matching" / f"{name}.debug"
        _split_debug(native_full, native, native_debug)
        other = _cc(out / "full" / f"mismatched-{name}",
                    SOURCES / "silproof_native.c", shared=True,
                    extra=["-DSILPROOF_MISMATCH"])
        mismatched_debug = debug / "mismatched" / f"{name}.debug"
        subprocess.run(["objcopy", "--only-keep-debug", str(other),
                        str(mismatched_debug)], check=True)
    else:
        shutil.copy2(native_full, native)
    fmu_binary = _cc(out / "full" / f"EgoMotion{suffix}",
                     SOURCES / "silproof_fmu.c", shared=True)
    return Artifacts(
        native=native, native_full=native_full, native_debug=native_debug,
        native_mismatched_debug=mismatched_debug,
        process=_cc(lib / f"libsilproof_process{suffix}",
                    SOURCES / "silproof_process.c", shared=True),
        child=_cc(bin_ / "silproof_child", SOURCES / "silproof_child.c",
                  shared=False),
        fmu=_package_fmu(fmu_binary, out / "EgoMotion.fmu"),
        fmu_binary=fmu_binary,
        adapter=SOURCES / "process_adapter.py",
    )


def manifest(artifacts: Artifacts, *, python: str = sys.executable,
             launch_prefix: dict[str, list[str]] | None = None) -> Manifest:
    """The proof Run. `launch_prefix` maps a Process participant name to
    the command words put before its command."""
    prefix = launch_prefix or {}
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas(SCHEMAS)
    m.add_channel("proof.native", schema="proof.Native")
    m.add_channel("proof.process", schema="proof.Process")
    m.add_channel("fmu.In", schema="fmu.In")
    m.add_channel("fmu.Out", schema="fmu.Out")
    m.add_native("native", library=str(artifacts.native),
                 publishes=["proof.native", "fmu.In"])
    m.add_process(
        "process",
        command=[*prefix.get("process", []), python, str(artifacts.adapter),
                 str(artifacts.process), str(artifacts.child)],
        step_period_ns=PERIOD_NS, publishes=["proof.process"], shim=True)
    m.add_process(
        "fmu",
        command=[*prefix.get("fmu", []), python, "-m", "sil.fmi",
                 str(artifacts.fmu)],
        step_period_ns=PERIOD_NS,
        subscribes=[SubscriberRoute("fmu.In", capacity=2)],
        publishes=["fmu.Out"], priority=1)
    return m
