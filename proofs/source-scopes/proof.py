"""Source-scope qualification with the ADAS reference (#372): Q1, Q20, Q21.

Linux x86-64/glibc and GCC 12 only (GitHub Actions ubuntu-24.04). The
proof builds two libraries from a copy of examples/adas-reference: the
original and an instrumented build in which the four functions of the #320
proof have one `SIL_SOURCE_SCOPE` each (include/sil/source_scope.h). The
shipped example stays unchanged.

Expected values are #320's worked example, independent of the decoder: one
and eight radar and camera objects give (check_object, confirmed, confirms,
select_lead) = (2, 1, 1, 1) and (16, 8, 36, 1) calls per Activation, in the
order check_object x 2N, select_lead, then per radar object i: confirmed,
confirms x (i + 1).

Three repeats of: sil-run with the original build, sil-run with the
instrumented build (a dormant observer), and one `sil profile --collector
scopes` capture per maneuver. All Recordings must be byte-identical and all
exit codes 0. Elapsed host time and storage are retained without a
threshold.
"""
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
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXAMPLE = ROOT / "examples/adas-reference"
SCOPES = {1: "check_object", 2: "confirmed", 3: "confirms", 4: "select_lead"}
OBJECTS = {"one": 1, "eight": 8}
REPEATS = 3
TIMEOUT_MS = "10000"  # generous Response deadline, as in #320
FLAGS = ["-std=c11", "-O2", "-g", "-fPIC", "-ffp-contract=off",
         "-Wl,--build-id=sha1"]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load example module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command, *, env=None, cwd=None, check=True):
    done = subprocess.run([str(x) for x in command], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=600)
    if check and done.returncode:
        raise RuntimeError(f"{command}: {done.returncode}\n{done.stderr}")
    return done


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def instrument(source: str) -> str:
    """The four functions with one scope each, as a source owner writes
    them. A changed anchor fails the proof instead of moving a scope."""
    result = source.replace('#include "adas_reference.h"',
                            '#include "adas_reference.h"\n'
                            "#include <sil/source_scope.h>", 1)
    for scope, name in SCOPES.items():
        pattern = rf"(static [^;{{}}]*\b{name}\([^;{{}}]*\)\s*\{{)"
        result, count = re.subn(
            pattern, rf'\1\n  SIL_SOURCE_SCOPE({scope}, "{name}");', result)
        if count != 1:
            raise ValueError(f"source anchor changed: {name}")
    return result


def inlined(binary: Path) -> dict:
    """DW_TAG_inlined_subroutine entries per scoped function, from DWARF."""
    text = run(["readelf", "--debug-dump=info", binary]).stdout
    names = {}
    for match in re.finditer(r"<(\w+)>: Abbrev Number: \d+ \(DW_TAG_subprogram"
                             r"\)\n(?:.*\n){0,3}?.*DW_AT_name\s*: (?:\(.*\): )?"
                             r"(\w+)", text):
        names[match[1]] = match[2]
    counts = {name: 0 for name in SCOPES.values()}
    for match in re.finditer(r"DW_TAG_inlined_subroutine\)\n(?:.*\n){0,2}?"
                             r".*DW_AT_abstract_origin.*<0x(\w+)>", text):
        name = names.get(match[1])
        if name in counts:
            counts[name] += 1
    return counts


def build(out: Path, observer_dir: Path) -> dict:
    """The original and the instrumented library, with identities and the
    inline decisions of both builds (Q20)."""
    sources = out / "sources"
    shutil.copytree(EXAMPLE, sources,
                    ignore=shutil.ignore_patterns("fmu", "maneuvers"))
    run([sys.executable, ROOT / "tools/silschema.py", sources / "schemas.json",
         sources / "adas_messages.h"])
    original = (sources / "adas_reference.c").read_text()
    (sources / "instrumented.c").write_text(instrument(original))
    includes = ["-I", ROOT / "include", "-I", sources]
    builds = {
        "original": [*FLAGS, *includes,
                     f"-fopt-info-inline-optimized={out / 'original.inline.txt'}",
                     "-shared", sources / "adas_reference.c",
                     sources / "sil_adapter.c", "-lm",
                     "-o", out / "original.so"],
        "instrumented": [*FLAGS, *includes, "-DSIL_SOURCE_SCOPES=1",
                         f"-fopt-info-inline-optimized="
                         f"{out / 'instrumented.inline.txt'}",
                         "-shared", sources / "instrumented.c",
                         sources / "sil_adapter.c", f"-L{observer_dir}",
                         "-lsil_source_scope", f"-Wl,-rpath,{observer_dir}",
                         "-lm", "-o", out / "instrumented.so"]}
    identities = {}
    for name, flags in builds.items():
        command = ["gcc-12", *flags]
        run(command)
        binary = out / f"{name}.so"
        notes = run(["readelf", "-n", binary]).stdout
        build_id = re.search(r"Build ID: (\w+)", notes)[1]
        dynamic = run(["readelf", "-d", binary]).stdout
        identities[name] = {
            "command": [str(x) for x in command], "sha256": sha256(binary),
            "build_id": build_id,
            "needed": re.findall(r"\(NEEDED\).*\[(.+)\]", dynamic),
            "inlined_subroutines": inlined(binary),
            "inline_remarks": f"{name}.inline.txt"}
    return identities


def write_inputs(inputs: Path) -> None:
    """#320's inputs: radar object i finds camera object i after i + 1
    tries; distinct pairs are 10 m apart."""
    prepare = load("scope_prepare", EXAMPLE / "prepare.py")
    inputs.mkdir()
    for name, count in OBJECTS.items():
        source = inputs / f"{name}.csv"
        with source.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time_ms", "radar", "camera", "ego_speed_mps"])
            radar = ";".join(f"{i} {40 + 10 * i} 0 0 0.875"
                             for i in range(count))
            camera = ";".join(f"{100 + i} {40.5 + 10 * i} 0 0.75"
                              for i in range(count))
            for slot in range(20):
                writer.writerow([slot * 10, radar, camera, 20])
        prepare.prepare_inputs(source, name, inputs)


def expected_order(n: int) -> list[int]:
    order = [1] * (2 * n) + [4]
    for i in range(n):
        order += [2] + [3] * (i + 1)
    return order


def check_counts(calls: dict, maneuver: str) -> None:
    """Q1: #320's counts and order in every Activation of the maneuver."""
    n = OBJECTS[maneuver]
    expected = {"1": 2 * n, "2": n, "3": n * (n + 1) // 2, "4": 1}
    activations = calls["activations"]
    if calls["status"] != "complete" or len(activations) != 20:
        raise ValueError(f"incomplete capture of {maneuver}")
    for activation in activations:
        if activation["status"] != "exact" or \
                activation["counts"] != expected or \
                [c["scope"] for c in activation["calls"]] != expected_order(n):
            raise ValueError(f"wrong counts or order: {activation}")


def storage(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def experiment(out: Path, build_dir: Path) -> None:
    out.mkdir(parents=True)
    identities = build(out, build_dir)
    inputs = out / "inputs"
    write_inputs(inputs)
    active = out / "active.so"
    manifest = out / "manifest.json"
    adas = load("scope_manifest", EXAMPLE / "manifest.py")
    adas.reference_manifest(inputs, active,
                            maneuvers=tuple(OBJECTS)).write(manifest)
    env = {**os.environ, "PATH": f"{build_dir}{os.pathsep}{os.environ['PATH']}",
           "PYTHONPATH": str(ROOT / "python/src")}
    env.pop("LD_PRELOAD", None)
    results = {
        "source_revision": run(["git", "-C", ROOT, "rev-parse", "HEAD"]
                               ).stdout.strip(),
        "machine": platform.uname()._asdict(),
        "glibc": platform.libc_ver()[1],
        "gcc": run(["gcc-12", "--version"]).stdout.splitlines()[0],
        "builds": identities,
        "runner_sha256": {name: sha256(build_dir / name) for name in (
            "sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher",
            "libsil_source_scope.so.1")},
        "manifest_sha256": sha256(manifest),
        "runs": []}
    recordings = set()
    for repeat in range(REPEATS):
        for mode in ("ordinary-original", "ordinary-instrumented",
                     "capture-one", "capture-eight"):
            variant = "original" if mode == "ordinary-original" \
                else "instrumented"
            shutil.copy2(out / f"{variant}.so", active)
            dest = out / f"{mode}-{repeat}"
            if mode.startswith("ordinary"):
                dest.mkdir()
                recording = dest / "run.mcap"
                command = [build_dir / "sil-run", manifest, "-o", recording,
                           "--participant-timeout-ms", TIMEOUT_MS]
            else:
                maneuver = mode.removeprefix("capture-")
                recording = dest / "run.mcap"
                command = [sys.executable, "-m", "sil.cli", "profile",
                           manifest, "--out", dest, "--collector", "scopes",
                           "--participant", maneuver, "--original-library",
                           out / "original.so", "--participant-timeout-ms",
                           TIMEOUT_MS]
            started = time.monotonic_ns()
            done = run(command, env=env, cwd=out, check=False)
            elapsed = time.monotonic_ns() - started
            (out / f"{mode}-{repeat}.log").write_text(done.stdout +
                                                      done.stderr)
            if done.returncode != 0:
                raise RuntimeError(f"{mode}-{repeat}: exit {done.returncode}"
                                   f"\n{done.stderr}")
            row = {"mode": mode, "repeat": repeat, "exit_code": done.returncode,
                   "elapsed_host_ns": elapsed,
                   "library_sha256": sha256(active),
                   "recording_sha256": sha256(recording),
                   "storage_bytes": storage(dest)}
            recordings.add(row["recording_sha256"])
            if mode.startswith("capture"):
                calls = json.loads((dest / "scopes/calls.json").read_text())
                report = json.loads((dest / "scopes.json").read_text())
                check_counts(calls, maneuver)
                if report["capture"]["status"] != "complete" or \
                        report["identities"]["original"]["sha256"] != \
                        identities["original"]["sha256"] or \
                        report["identities"]["instrumented"]["sha256"] != \
                        identities["instrumented"]["sha256"]:
                    raise ValueError(f"capture identities: {dest}")
                row["capture"] = report["capture"]
                row["summary"] = report["summary"]
            results["runs"].append(row)
            write(out / "results.json", results)
    if len(recordings) != 1:
        raise ValueError(f"Recordings differ: {sorted(recordings)}")
    results["recording_sha256"] = recordings.pop()
    write(out / "results.json", results)
    digests = [f"{sha256(p)}  {p.relative_to(out)}"
               for p in sorted(out.rglob("*")) if p.is_file()]
    (out / "digests.txt").write_text("\n".join(digests) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--build", type=Path, required=True,
                        help="the CMake build directory (GCC 12)")
    args = parser.parse_args()
    if (platform.system(), platform.machine(), platform.libc_ver()[0]) != \
            ("Linux", "x86_64", "glibc"):
        parser.error("the evidence needs Linux x86-64/glibc (ubuntu-24.04)")
    experiment(args.out.resolve(), args.build.resolve())
