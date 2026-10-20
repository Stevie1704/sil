"""Bounded external-GDB feasibility experiment for issue #321.

No product command, Manifest field, ABI, protocol or kernel hook is added.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from sil.profile_modules import ModuleLocator, elf_identity
from sil.recording import read_records
from sil.schema import MessageType

from validate import validate_capture

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EXAMPLE = ROOT / "examples/adas-reference"
DEADLINE_MS = 30_000
RUN_LIMIT_S = 60
SHORT_DEADLINE_MS = 2_000


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load example module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command):
    result = subprocess.run([str(x) for x in command], capture_output=True,
                            text=True, timeout=RUN_LIMIT_S, check=True)
    return result.stdout + result.stderr


def separate(binary):
    debug = Path(str(binary) + ".debug")
    run(["objcopy", "--only-keep-debug", binary, debug])
    run(["strip", "--strip-debug", binary])
    run(["objcopy", f"--add-gnu-debuglink={debug}", binary])
    identity, debug_identity = elf_identity(binary), elf_identity(debug)
    if not identity or not debug_identity or not identity.build_id:
        raise ValueError(f"missing ELF identity: {binary}")
    if identity.has_debug_lines or identity.build_id != debug_identity.build_id:
        raise ValueError(f"debug separation failed: {binary}")
    facts = ModuleLocator().facts(str(binary), identity.build_id)
    if facts.debug_status != "separate_matching":
        raise ValueError(f"separate debug not matched: {facts}")
    return {"path": str(binary), "sha256": digest(binary),
            "debug_sha256": digest(debug), "elf": dataclasses.asdict(identity),
            "debug_elf": dataclasses.asdict(debug_identity),
            "match": dataclasses.asdict(facts)}


def build_workload(out, build):
    sources = out / "sources"
    shutil.copytree(EXAMPLE, sources)
    run([sys.executable, ROOT / "tools/silschema.py", sources / "schemas.json",
         sources / "adas_messages.h"])
    commands = []
    flags = ["gcc-12", "-std=c11", "-O2", "-g", "-fPIC", "-shared",
             "-ffp-contract=off", "-Wl,--build-id=sha1", "-I", ROOT / "include",
             "-I", sources]
    library = out / "adas.so"
    commands.append([*flags, sources / "adas_reference.c", sources / "sil_adapter.c",
                     "-lm", "-o", library])
    fmu_library = out / "AdasReference.so"
    token = "source-debugging-proof-321"
    commands.append([*flags, "-fvisibility=hidden", "-I", sources / "fmu/fmi3",
                     f'-DADAS_FMU_INSTANTIATION_TOKEN="{token}"',
                     sources / "adas_reference.c", sources / "fmu/fmi3_controller.c",
                     "-lm", "-o", fmu_library])
    for command in commands:
        run(command)
    write(out / "build-commands.json", [[str(x) for x in c] for c in commands])
    identities = [separate(library), separate(fmu_library)]
    # GDB resolves the extracted FMU's debuglink through this explicit path.
    with zipfile.ZipFile(out / "AdasReference.fmu", "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("modelDescription.xml", (sources / "fmu/modelDescription.xml")
                         .read_bytes().replace(b"@INSTANTIATION_TOKEN@", token.encode()))
        archive.write(fmu_library, "binaries/x86_64-linux/AdasReference.so")
        archive.write(Path(str(fmu_library) + ".debug"),
                      "binaries/x86_64-linux/AdasReference.so.debug")
    for name in ("sil-run", "sil-run-diagnostics", "libsil_clock_shim.so"):
        binary = out / name
        shutil.copy2(build / name, binary)
        identities.append(separate(binary))
    write(out / "identities.json", identities)
    return library


def attach_probe(out):
    # A sibling attach is deliberately distinct from launch-under-GDB.
    with subprocess.Popen(["sleep", "10"]) as target:
        try:
            result = subprocess.run(["gdb", "-nx", "-q", "-batch", "-ex",
                f"attach {target.pid}", "-ex", "detach"],
                capture_output=True, text=True, timeout=5)
            write(out / "attach.json", {"exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
                "ptrace_scope": Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip(),
                "policy": "no sysctl, capability or security-policy change; launch regardless"})
        finally:
            target.terminate()
            target.wait(timeout=5)


def capture(out, manifest, form, label, *, selector=None, action="resume"):
    dest = out / "runs" / f"{form}-{label}"
    dest.mkdir(parents=True)
    diagnostic = selector is not None or label == "observations"
    command = [out / ("sil-run-diagnostics" if diagnostic else "sil-run"), manifest,
               "-o", dest / "recording.mcap", "--participant-timeout-ms",
               str(SHORT_DEADLINE_MS if action == "deadline" else DEADLINE_MS)]
    if diagnostic:
        command += ["--diagnostics-out", dest / "diagnostics.json",
                    "--activation-observations", dest / "observations.jsonl",
                    "--activation-observations-limit-bytes", str(1 << 20)]
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if selector:
        commands = dest / "session.gdb"
        logging = dest / "logging.gdb"
        # Redirect GDB's own output while preserving inferior protocol stdout.
        # Initial commands run before loading executable symbols/startup output.
        logging.write_text(
            f"set logging file {dest / 'gdb.txt'}\nset logging overwrite on\n"
            "set logging redirect on\nset logging enabled on\n")
        commands.write_text(f"source {HERE / 'session.py'}\n")
        env.update(SIL_DEBUG_DEST=str(dest), SIL_DEBUG_SELECTOR=selector,
                   SIL_DEBUG_ACTION=action, SIL_DEBUG_COMMANDS=str(commands),
                   SIL_DEBUG_LOGGING=str(logging),
                   SIL_DEBUG_SOURCE=str(out / "sources/adas_reference.c"))
        if form == "native":
            child_result = [] if action == "detach" else ["--return-child-result"]
            command = ["gdb", *child_result, "-nx", "-q", "-batch", "-ix", logging,
                       "-x", commands, "--args", *command]
        else:
            command += ["--launch-wrapper", "hazard", HERE / "launch.sh"]
    started = time.monotonic_ns()
    timed_out = False
    with subprocess.Popen([str(x) for x in command], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=RUN_LIMIT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            # A Process participant owns a separate process group.
            stops_file = dest / "stops.json"
            if stops_file.exists():
                for row in json.loads(stops_file.read_text()):
                    try:
                        os.killpg(row["pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            stdout, stderr = process.communicate(timeout=5)
    (dest / "stdout.txt").write_text(stdout)
    (dest / "stderr.txt").write_text(stderr)
    result = {"form": form, "label": label, "selector": selector, "action": action,
              "command": [str(x) for x in command], "exit_code": process.returncode,
              "elapsed_host_ns": time.monotonic_ns() - started,
              "deadline_ms": SHORT_DEADLINE_MS if action == "deadline" else DEADLINE_MS,
              "manifest_sha256": digest(manifest)}
    result["supervisor_timeout"] = timed_out
    write(dest / "result.json", result)
    if timed_out:
        raise TimeoutError(f"supervisor timeout; partial evidence retained: {dest}")
    stops_file = dest / "stops.json"
    stops = json.loads(stops_file.read_text()) if stops_file.exists() else []
    if selector and (not stops or stops[0]["stage"] != "entry"):
        raise ValueError(f"no selected-Activation debugger evidence: {dest}")
    diagnostics_path = dest / "diagnostics.json"
    result["run_outcome"] = (json.loads(diagnostics_path.read_text())["run"]
                             if diagnostics_path.exists() else
                             {"exit_code": None, "reason": "runner report unavailable"})
    if selector and action != "resume":
        marker = json.loads((dest / "action.json").read_text())
        if marker["action"] != action or marker["index"] != 4:
            raise ValueError("control action did not reach the selected Activation")
        log = (dest / "gdb.txt").read_text()
        if action == "debugger-error" and "deliberate debugger failure" not in log + stderr:
            raise ValueError("missing deliberate debugger failure diagnostic")
        if action == "signal" and form != "native":
            debugger_outcome = json.loads((dest / "debugger-outcome.json").read_text())
            # sil.participant installs a handler that unwinds Python and exits 1.
            if (debugger_outcome["exit_code"]["value"] != "1" or
                    "participant terminated by signal 15" not in stderr):
                raise ValueError("SIGTERM did not unwind the Python participant")
        if action == "kill" and "killed" not in log:
            raise ValueError("GDB did not report killing the inferior")
        failure = result["run_outcome"].get("failure") or {}
        if not isinstance(failure, dict):
            raise ValueError("invalid Run failure evidence")
        if action == "deadline" and (failure.get("category") != "timeout" or
                failure.get("virtual_time_ns") != 40_000_000):
            raise ValueError("deadline control did not fail the selected Step response")
        if action == "signal" and form == "native" and failure.get("category") != "interrupted":
            raise ValueError("Native SIGTERM did not produce an interrupted Run")
        if action == "signal" and form != "native" and (
                failure.get("category") != "exit" or failure.get("virtual_time_ns") != 40_000_000):
            raise ValueError("Python SIGTERM did not fail the selected Step")
    # A reaped process must have no surviving observed inferior, including detach.
    for pid in {row["pid"] for row in stops}:
        if Path(f"/proc/{pid}").exists():
            raise ValueError(f"inferior survived Run cleanup: {pid}")
    result["observed_inferiors_reaped"] = True
    if action in ("resume", "detach"):
        if process.returncode:
            raise ValueError(f"Run failed: {dest}: {stderr}")
        result["recording_sha256"] = digest(dest / "recording.mcap")
    elif process.returncode == 0:
        raise ValueError(f"negative control unexpectedly succeeded: {dest}")
    if selector and action == "resume":
        validate_capture(dest, form)
        result["activation_stops_verified"] = True
    write(dest / "result.json", result)
    return result


def experiment(out, build):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("real qualification requires Linux x86-64/glibc on ubuntu-24.04")
    out.mkdir(parents=True, exist_ok=False)
    report = {"source_revision": run(["git", "rev-parse", "HEAD"]).strip(),
              "ci_run": os.environ.get("GITHUB_RUN_ID"), "host": platform.platform(),
              "gdb": run(["gdb", "--version"]), "compiler": run(["gcc-12", "--version"]),
              "glibc": run(["ldd", "--version"]), "runs": []}
    write(out / "report.json", report)
    library = build_workload(out, build)
    attach_probe(out)
    prepare = load("debug_prepare", EXAMPLE / "prepare.py")
    manifest = load("debug_manifest", EXAMPLE / "manifest.py")
    equivalence = load("debug_equivalence", ROOT / "proofs/adas-equivalence/experiment.py")
    inputs = out / "inputs"
    prepare.prepare(inputs, maneuvers=("hazard",))
    command_type = MessageType("adas.Command", manifest.SCHEMAS["adas.Command"])
    for form in ("native", "process", "fmu", "shim"):
        if form == "native":
            controller = manifest.native_controller(library)
        elif form == "fmu":
            controller = equivalence.fmu_controller(out / "AdasReference.fmu")
        else:
            def controller(m, name, routes):
                m.add_process(name, command=[sys.executable, str(EXAMPLE / "process_adapter.py"),
                    str(library), json.dumps(manifest.controller_config(name), sort_keys=True)],
                    step_period_ns=10_000_000, subscribes=routes,
                    publishes=["hazard.command"], shim=form == "shim")
        m = manifest.reference_manifest(inputs, library, maneuvers=("hazard",), controller=controller)
        path = out / f"{form}.json"
        m.write(path)
        baseline = None
        for label, selector, action in (
            ("ordinary-0", None, "resume"), ("ordinary-1", None, "resume"),
            ("observations", None, "resume"), ("index", "index", "resume"),
            ("time", "time", "resume"), ("detach", "time", "detach"),
            ("kill", "time", "kill"), ("signal", "time", "signal"),
            ("debugger-error", "time", "debugger-error"),
            *(([("deadline", "time", "deadline")]) if form != "native" else [])):
            row = capture(out, path, form, label, selector=selector, action=action)
            if "recording_sha256" in row:
                baseline = baseline or row["recording_sha256"]
                if baseline != row["recording_sha256"]:
                    raise ValueError(f"Recording changed after debugging: {form}/{label}")
                row["recording_equal"] = True
                records = [(t, command_type.unpack(data)) for channel, t, data in
                           read_records(out / "runs" / f"{form}-{label}" / "recording.mcap")
                           if channel == "hazard.command"]
                if len(records) != 20 or records[4][1]["acceleration_mps2"] != -2.5 or records[5][1]["acceleration_mps2"] != -3:
                    raise ValueError("Recording does not match named hazard fixture")
            report["runs"].append(row)
            write(out / "report.json", report)
    # Test the ticket's grouped-ADAS premise without expanding the product.
    def group_controller(m, name, routes):
        m.add_process(name, command=[sys.executable, "-m", "sil.fmi", "--instance", "adas",
            str(out / "AdasReference.fmu"), "--instance", "peer", str(out / "AdasReference.fmu"),
            "--bus-profile", "application/org.fmi-standard.fmi-ls-bus.can",
            "--bind", "hazard.radar:sample_time_ns=adas.radar.sample_time_ns"], step_period_ns=10_000_000,
            subscribes=routes, publishes=["hazard.command"])
    group = manifest.reference_manifest(inputs, library, maneuvers=("hazard",),
                                       controller=group_controller)
    group.write(out / "group.json")
    row = capture(out, out / "group.json", "group", "unsupported", action="reject")
    group_error = (out / "runs/group-unsupported/stderr.txt").read_text()
    if row["exit_code"] != 2 or "member of a terminal" not in group_error:
        raise ValueError("group failed for a reason other than missing BUS terminals")
    report["grouped_adas"] = row
    report["decision"] = "no-change: external scripts suffice for qualified forms; grouped ADAS deferred"
    write(out / "report.json", report)
    paths = sorted(p for p in out.rglob("*") if p.is_file())
    (out / "digests.txt").write_text("".join(f"{digest(p)}  {p.relative_to(out)}\n" for p in paths))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()
    experiment(args.out.resolve(), args.build.resolve())
