#!/usr/bin/env python3
"""Observe bpftrace availability without changing kernel/container policy."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

from evidence import digest, save


PACKAGE = "0.20.2-1ubuntu4.3"
ALLOCATIONS = 50_000
FIXTURE = r"""#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
static uint64_t now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) exit(2);
    return (uint64_t)t.tv_sec * 1000000000ull + t.tv_nsec;
}
__attribute__((noinline)) int proof_malloc_site(void) {
    for (unsigned i = 0; i < 50000; ++i) {
        void *p = malloc(2048);
        if (!p) return 2;
        ((volatile unsigned char *)p)[0] = (unsigned char)i;
        __asm__ volatile("" : : "r"(p) : "memory");
        free(p);
    }
    return 0;
}
int main(void) {
    uint64_t start = now();
    int rc = proof_malloc_site();
    printf("FIXTURE_NS %llu UID %u RC %d\n",
           (unsigned long long)(now() - start), (unsigned)getuid(), rc);
    return rc;
}
"""


def _read(path: str) -> dict:
    try:
        return {"value": Path(path).read_text().strip()}
    except OSError as exc:
        return {"error": str(exc)}


def facts() -> dict:
    status = _read("/proc/self/status")
    selected = {}
    for line in status.get("value", "").splitlines():
        key, _, value = line.partition(":")
        if key in {"Uid", "Gid", "CapInh", "CapPrm", "CapEff", "CapBnd",
                   "CapAmb", "NoNewPrivs", "Seccomp", "Seccomp_filters"}:
            selected[key] = value.strip()
    return {
        "system": platform.system(), "machine": platform.machine(),
        "kernel": platform.release(), "uid": os.getuid(),
        "euid": os.geteuid(), "process_status": selected,
        "policy": {name: _read(path) for name, path in {
            "unprivileged_bpf_disabled": "/proc/sys/kernel/unprivileged_bpf_disabled",
            "perf_event_paranoid": "/proc/sys/kernel/perf_event_paranoid",
            "ptrace_scope": "/proc/sys/kernel/yama/ptrace_scope",
            "lockdown": "/sys/kernel/security/lockdown",
        }.items()},
        "interfaces": {path: Path(path).exists() for path in (
            "/sys/kernel/btf/vmlinux", "/sys/kernel/tracing",
            "/sys/kernel/debug/tracing", f"/lib/modules/{platform.release()}")},
        "container_marker": Path("/.dockerenv").exists(),
        "bpftrace_environment": {key: value for key, value in os.environ.items()
                                 if key.startswith("BPFTRACE_")},
    }


def _run(args: list[str], out: Path, name: str, timeout: int = 45) -> dict:
    """Keep full logs and terminate the owned tracer/fixture group on timeout."""
    stdout = out / f"{name}.stdout.txt"
    stderr = out / f"{name}.stderr.txt"
    start = time.monotonic_ns()
    timed_out = False
    with stdout.open("w") as output, stderr.open("w") as errors:
        try:
            child = subprocess.Popen(args, stdout=output, stderr=errors,
                                     start_new_session=True)
        except OSError as exc:
            errors.write(str(exc))
            return {"argv": args, "returncode": None, "error": str(exc),
                    "stdout": stdout.name, "stderr": stderr.name}
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return {"argv": args, "returncode": child.returncode,
            "timed_out": timed_out,
            "wall_ns": time.monotonic_ns() - start,
            "stdout": stdout.name, "stderr": stderr.name}


def _text(out: Path, result: dict) -> str:
    return (out / result["stdout"]).read_text() + (out / result["stderr"]).read_text()


def _fixture_ns(out: Path, result: dict) -> int | None:
    match = re.search(r"FIXTURE_NS (\d+) UID \d+ RC 0", _text(out, result))
    return int(match[1]) if match and result["returncode"] == 0 else None


def probe(out: Path) -> dict:
    """Write a standalone report; permission denial is an observed result."""
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "availability.json").exists():
        raise FileExistsError(out / "availability.json")
    report = {"schema": 1, "facts": facts(), "expected_package": PACKAGE,
              "commands": {}, "attempts": [], "support_decision": "defer",
              "scope": "malloc fixture availability, not a SiL collector qualification"}

    def finish(status: str) -> dict:
        report["status"] = status
        report["files"] = {path.name: digest(path) for path in sorted(out.iterdir())
                           if path.is_file() and path.name != "availability.json"}
        save(out / "availability.json", report)
        return report

    if platform.system() != "Linux":
        return finish("unsupported_host")
    report["qualification_machine"] = platform.machine() == "x86_64"
    tool = shutil.which("bpftrace")
    if not tool:
        return finish("bpftrace_missing")
    compiler = shutil.which(os.environ.get("CC", "cc"))
    if not compiler:
        return finish("compiler_missing")
    commands = report["commands"]
    commands["version"] = _run([tool, "--version"], out, "version")
    commands["package"] = _run(
        ["dpkg-query", "-W", "-f=${Version}\n", "bpftrace"], out, "package")
    report["package_matches_pin"] = (
        _text(out, commands["package"]).strip() == PACKAGE)
    if not report["package_matches_pin"]:
        return finish("package_pin_mismatch")
    commands["info"] = _run([tool, "--info"], out, "info")
    commands["compiler_version"] = _run([compiler, "--version"], out, "compiler-version")
    commands["library_packages"] = _run(["dpkg-query", "-W", "libc6", "libstdc++6"], out, "library-packages")
    source = out / "malloc-fixture.c"
    fixture = out / "malloc-fixture"
    source.write_text(FIXTURE)
    commands["compile"] = _run([
        compiler, "-O2", "-g", "-fno-omit-frame-pointer", "-fno-inline",
        "-fno-builtin-malloc", "-fno-builtin-free", "-rdynamic", str(source),
        "-o", str(fixture)], out, "compile")
    if commands["compile"]["returncode"] != 0:
        return finish("fixture_compile_failed")
    commands["ldd"] = _run(["ldd", str(fixture)], out, "ldd")
    match = re.search(r"libc\.so\.6 => (\S+)", _text(out, commands["ldd"]))
    if not match:
        return finish("glibc_path_unavailable")
    libc = match[1]
    # The marker isolates calls in the known loop from process initialization.
    # It does not claim startup coverage for arbitrary participants.
    program = out / "malloc.bt"
    program.write_text(f"""uprobe:{fixture}:proof_malloc_site /pid == cpid/ {{ @active[tid] = 1; }}
uretprobe:{fixture}:proof_malloc_site /pid == cpid/ {{ delete(@active[tid]); }}
uprobe:{libc}:malloc /pid == cpid && @active[tid] && arg0 == 2048/ {{
  @calls = count(); @first = min(nsecs(monotonic)); @last = max(nsecs(monotonic));
  @stacks[ustack(12)] = count();
}}
END {{ clear(@active); }}
""")

    def attempt(name: str, prefix: list[str]) -> dict:
        context = _run(prefix + [sys.executable, str(Path(__file__).resolve()),
                                 "--facts"], out, f"{name}-facts")
        baseline = _run(prefix + [str(fixture)], out, f"{name}-baseline")
        # A root-owned tracer receives its timeout signal from root-owned
        # coreutils timeout, even when this script itself is unprivileged.
        bounded = ["timeout", "--signal=TERM", "--kill-after=5", "35"] if prefix else []
        capture = _run(prefix + bounded + [tool, "-k", str(program), "-c",
                                  shlex.quote(str(fixture))], out, f"{name}-capture")
        output = _text(out, capture)
        calls = re.search(r"^@calls:\s+(\d+)\s*$", output, re.MULTILINE)
        count = int(calls[1]) if calls else None
        stack = bool(re.search(r"^\s+proof_malloc_site(?:\+|\s)", output, re.MULTILINE))
        observed_ns = _fixture_ns(out, capture)
        baseline_ns = _fixture_ns(out, baseline)
        diagnostic_error_or_loss = bool(re.search(
            r"(?:ERROR:|WARNING: Failed|[Ll]ost \d+ events)", output))
        complete = (capture["returncode"] == 0 and count == ALLOCATIONS
                    and stack and observed_ns is not None
                    and not diagnostic_error_or_loss)
        row = {"context": name, "facts_command": context, "baseline": baseline,
               "capture": capture, "known_calls_expected": ALLOCATIONS,
               "known_calls_observed": count, "known_frame_resolved": stack,
               "diagnostic_error_or_loss": diagnostic_error_or_loss,
               "status": "fixture_captured" if complete else "capture_unavailable_or_incomplete",
               "fixture_baseline_ns": baseline_ns, "fixture_profiled_ns": observed_ns,
               "output_bytes": (out / capture["stdout"]).stat().st_size,
               "clock": None,
               "configured_clock": "nsecs(monotonic), source maps to kernel bpf_ktime_get_ns",
               "overhead_scope": "one loop timing per mode; no startup/attach time or statistical estimate"}
        for field in ("first", "last"):
            timestamp = re.search(rf"^@{field}:\s+(\d+)\s*$", output, re.MULTILINE)
            row[f"kernel_{field}_ns"] = int(timestamp[1]) if timestamp else None
        if row["kernel_first_ns"] is not None and row["kernel_last_ns"] is not None:
            row["clock"] = "host monotonic via bpf_ktime_get_ns; source-identified helper with observed values"
        if complete and baseline_ns:
            row["single_run_loop_time_ratio"] = observed_ns / baseline_ns
        report["attempts"].append(row)
        return row

    current = attempt("current-user", [])
    # This uses only already available noninteractive sudo; it grants no new
    # capability and changes no sysctl, mount, seccomp or container setting.
    if os.geteuid() != 0 and current["status"] != "fixture_captured" and shutil.which("sudo"):
        commands["sudo_check"] = _run(["sudo", "-n", "true"], out, "sudo-check")
        if commands["sudo_check"]["returncode"] == 0:
            attempt("sudo-root", ["sudo", "-n"])
    return finish("observed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", nargs="?", type=Path)
    parser.add_argument("--facts", action="store_true")
    args = parser.parse_args()
    if args.facts:
        print(json.dumps(facts(), indent=2))
    elif args.out is None:
        parser.error("out is required unless --facts is used")
    else:
        result = probe(args.out)
        print(json.dumps({"status": result["status"], "output": str(args.out)}))
        sys.exit(0 if result["status"] == "observed" else 1)
