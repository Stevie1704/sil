"""Repeatable issue #320 experiment, Linux x86-64/glibc only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from analyze import FUNCTIONS, analyze

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXAMPLE = ROOT / "examples/adas-reference"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command, *, env=None, timeout=60):
    result = subprocess.run([str(x) for x in command], env=env,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{command}: {result.returncode}\n{result.stderr}")
    return result.stdout + result.stderr


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def build(out):
    """Retain exact copied sources, commands, maps and inline diagnostics."""
    sources = out / "sources"
    shutil.copytree(EXAMPLE, sources)
    for name in ("probe.c", "probe.h", "control.cpp", "xray_attach.c"):
        shutil.copy2(HERE / name, sources / name)
    original = (sources / "adas_reference.c").read_text()
    instrumented = '#include "probe.h"\n' + original
    for identity, name in FUNCTIONS.items():
        pattern = rf"(static [^;{{}}]*\b{name}\([^;{{}}]*\)\s*\{{)"
        instrumented, count = re.subn(
            pattern, rf"\1\n  TRACE_SCOPE({identity});", instrumented)
        if count != 1:
            raise ValueError(f"source anchor changed: {name}")
    (sources / "explicit.c").write_text(instrumented)
    noinline = original
    for name in FUNCTIONS.values():
        noinline = re.sub(rf"(static )([^;{{}}]*\b{name}\()",
                          r"\1__attribute__((noinline)) \2", noinline)
    (sources / "noinline.c").write_text(noinline)
    # Default exclusion, explicit overrides for the four selected functions.
    # Enumerating the other source functions avoids conflicting wildcard rules.
    names = re.findall(r"^\w[\w *]*\b(\w+)\([^;{}]*\)\s*\{", original, re.M)
    attrs = "[always]\n" + "".join(f"fun:{n}\n" for n in FUNCTIONS.values())
    attrs += "[never]\nsrc:*sil_adapter.c\n" + "".join(f"fun:{n}\n" for n in names
                                  if n not in FUNCTIONS.values())
    (sources / "xray.txt").write_text(attrs)
    flags = ["-O2", "-g", "-fPIC", "-ffp-contract=off", "-I", ROOT / "include",
             "-I", sources, "-Wl,--build-id=sha1"]
    commands = []
    variants = {"gcc": ("gcc-12", "adas_reference.c"),
                "explicit": ("gcc-12", "explicit.c"),
                "clang": ("clang-18", "adas_reference.c"),
                "xray": ("clang-18", "adas_reference.c"),
                "xray-noinline": ("clang-18", "noinline.c")}
    for variant, (compiler, source) in variants.items():
        extra = []
        if compiler == "gcc-12":
            extra += [f"-fopt-info-inline-optimized={out / (variant + '.inline.txt')}"]
        else:
            extra += ["-Rpass=inline"]
        if variant.startswith("xray"):
            extra += ["-fxray-instrument", "-fxray-instruction-threshold=1",
                      f"-fxray-attr-list={sources / 'xray.txt'}"]
        command = [compiler, *flags, *extra, "-shared", sources / source,
                   sources / "sil_adapter.c"]
        if variant == "explicit":
            command += [sources / "probe.c", "-ldl"]
        if variant.startswith("xray"):
            command += [sources / "xray_attach.c", "-ldl"]
        command += ["-lm", "-o", out / f"{variant}.so"]
        commands.append([str(x) for x in command])
        (out / f"{variant}.build.txt").write_text(run(command))
        (out / f"{variant}.symbols.txt").write_text(
            run(["nm", "-an", out / f"{variant}.so"]))
        (out / f"{variant}.debug.txt").write_text(
            run(["readelf", "-n", "-wi", out / f"{variant}.so"]))
        if variant.startswith("xray"):
            (out / f"{variant}.map.yaml").write_text(run([
                "llvm-xray-18", "extract", "--symbolize", out / f"{variant}.so"]))
    # A preloaded, uninstrumented observer supplies a custom XRay handler.
    # XRay runtime itself belongs to the proof-only executable, not the DSO.
    command = ["clang-18", *flags, "-shared", sources / "probe.c", "-ldl",
               "-o", out / "observer.so"]
    commands.append([str(x) for x in command])
    run(command)
    control_attrs = sources / "control.txt"
    control_attrs.write_text("[always]\nfun:*control*\n[never]\nfun:main\n")
    command = ["clang++-18", "-O2", "-g", "-fxray-instrument",
               "-fxray-instruction-threshold=1",
               f"-fxray-attr-list={control_attrs}", "-Wl,--export-dynamic",
               sources / "control.cpp", "-pthread", "-o", out / "control"]
    commands.append([str(x) for x in command])
    run(command)
    write(out / "build-commands.json", commands)


def experiment(out, gcc_build, xray_build):
    out.mkdir(parents=True, exist_ok=False)
    build(out)
    prepare = load("trace_prepare", EXAMPLE / "prepare.py")
    manifest = load("trace_manifest", EXAMPLE / "manifest.py")
    inputs = out / "inputs"
    inputs.mkdir()
    counts = {"one": 1, "eight": 8}
    # Geometrically separated pairs: radar i finds camera i after i+1 tries.
    for name, count in counts.items():
        source = inputs / f"{name}.csv"
        with source.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time_ms", "radar", "camera", "ego_speed_mps"])
            radar = ";".join(f"{i} {40+10*i} 0 0 0.875" for i in range(count))
            camera = ";".join(f"{100+i} {40.5+10*i} 0 0.75" for i in range(count))
            for slot in range(20):
                writer.writerow([slot * 10, radar, camera, 20])
        prepare.prepare_inputs(source, name, inputs)
    active = out / "active.so"
    manifest_path = out / "manifest.json"
    manifest.reference_manifest(inputs, active, maneuvers=tuple(counts)).write(manifest_path)
    results = {"machine": platform.uname()._asdict(), "objects_per_sensor": counts,
               "source_revision": run(["git", "-C", ROOT, "rev-parse", "HEAD"]).strip(),
               "tools": {tool: run([tool, "--version"]).splitlines()[0]
                         for tool in ("gcc-12", "clang-18", "llvm-xray-18")},
               "runs": []}
    baseline = None
    for mode in ("ordinary", "diagnostics", "explicit", "clang", "xray", "xray-noinline", "loss"):
        variant = {"ordinary": "gcc", "diagnostics": "gcc", "loss": "explicit"}.get(mode, mode)
        shutil.copy2(out / f"{variant}.so", active)
        for repeat in range(3 if mode != "loss" else 1):
            dest = out / f"{mode}-{repeat}"
            dest.mkdir()
            env = os.environ.copy()
            env.pop("LD_PRELOAD", None)
            env["SIL_FUNCTION_TRACE"] = str(dest / "events.bin")
            if mode.startswith("xray"):
                env.update(LD_PRELOAD=str(out / "observer.so"), SIL_FUNCTION_XRAY="1")
            if mode == "loss":
                env["SIL_FUNCTION_TRACE_LIMIT"] = "7"
            build_dir = xray_build if mode.startswith("xray") else gcc_build
            command = [build_dir / ("sil-run" if mode == "ordinary" else "sil-run-diagnostics"),
                       manifest_path, "-o", dest / "recording.mcap", "--participant-timeout-ms", "10000"]
            if mode != "ordinary":
                command += ["--diagnostics-out", dest / "diagnostics.json",
                            "--activation-observations", dest / "observations.jsonl",
                            "--activation-observations-limit-bytes", str(1 << 20)]
            started = time.monotonic_ns()
            (dest / "run.txt").write_text(run(command, env=env))
            elapsed = time.monotonic_ns() - started
            recording = (dest / "recording.mcap").read_bytes()
            if baseline is None:
                baseline = recording
            if recording != baseline:
                raise ValueError(f"Recording mismatch: {dest}")
            row = {"mode": mode, "repeat": repeat, "elapsed_host_ns": elapsed,
                   "library_variant": variant,
                   "library_sha256": hashlib.sha256(active.read_bytes()).hexdigest(),
                   "runner_sha256": hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
                   "recording_equal": True, "recording_sha256": hashlib.sha256(recording).hexdigest()}
            if (dest / "events.bin").exists():
                metadata = json.loads((dest / "events.bin.json").read_text())
                row["trace"] = metadata
                if mode in ("explicit", "loss"):
                    report = analyze(dest / "events.bin", dest / "observations.jsonl")
                    write(dest / "calls.json", report)
                    if mode == "explicit":
                        if report["status"] != "complete" or report["unattributed"]:
                            raise ValueError("incomplete source probe capture")
                        for a in report["activations"]:
                            n = counts[a["participant"]]
                            expected = {"check_object": 2*n, "confirmed": n,
                                        "confirms": n*(n+1)//2, "select_lead": 1}
                            if a["counts"] != expected:
                                raise ValueError(f"incorrect call counts: {a}")
                    elif report["status"] != "partial" or not metadata["dropped"]:
                        raise ValueError("loss was not explicit")
            results["runs"].append(row)
            write(out / "results.json", results)
    env = {**os.environ, "LD_PRELOAD": str(out / "observer.so"),
           "SIL_FUNCTION_TRACE": str(out / "control.bin"), "SIL_FUNCTION_XRAY": "1"}
    (out / "control.txt").write_text(run([out / "control"], env=env))
    write(out / "control.metadata.json", json.loads((out / "control.bin.json").read_text()))
    (out / "control.map.yaml").write_text(run([
        "llvm-xray-18", "extract", "--symbolize", out / "control"]))
    if not json.loads((out / "control.bin.json").read_text())["records"]:
        raise ValueError("XRay executable positive control produced no events")
    digests = []
    for path in sorted(out.rglob("*")):
        if path.is_file():
            digests.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(out)}")
    (out / "digests.txt").write_text("\n".join(digests) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--gcc-build", type=Path, required=True)
    parser.add_argument("--xray-build", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("real evidence requires Linux x86-64/glibc (ubuntu-24.04)")
    experiment(args.out.resolve(), args.gcc_build.resolve(), args.xray_build.resolve())
