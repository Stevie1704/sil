"""Disposable measured clock calibration; run after building the Clock shim."""
from __future__ import annotations

import argparse
import os
import platform
import struct
import subprocess
import time
from pathlib import Path

from evidence import command, digest, save

HERE = Path(__file__).resolve().parent
VALUES = [0, 100_000_000, 200_000_000, 400_000_000, 900_000_000]
RAW_BOUND = 32 * 1024 * 1024
WALL_BOUND = 5
RECORD = struct.Struct("<8Q")


def _samples(directory):
    maps = []
    for line in (directory / "maps.txt").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5].startswith("/"):
            start, end = (int(x, 16) for x in fields[0].split("-"))
            maps.append((start, end, int(fields[2], 16), fields[5]))
    symbols = {}
    for path in {row[3] for row in maps if "libstdc++" in row[3]}:
        result = command(["nm", "-D", "-S", "--defined-only", path])
        symbols[path] = []
        for line in result["stdout"].splitlines():
            fields = line.split()
            if len(fields) == 4:
                symbols[path].append((int(fields[0], 16), int(fields[1], 16), fields[3]))
        save(directory / "caller-symbols.json", result)
    data = (directory / "audit.bin").read_bytes()
    assert data and len(data) % RECORD.size == 0
    rows = []
    for sequence, tid, clock_id, caller, value, before, after, status in RECORD.iter_unpack(data):
        module, offset, symbol = None, None, None
        for start, end, file_offset, path in maps:
            if start <= caller < end:
                module, offset = path, caller - start + file_offset
                for address, size, name in symbols.get(path, []):
                    if address <= offset < address + size:
                        symbol = name
                        break
                break
        rows.append({"sequence": sequence, "tid": tid, "clock_id": clock_id,
                     "caller_address": caller, "module": module,
                     "module_offset": offset, "symbol": symbol,
                     "returned_ns": value, "host_before_ns": before,
                     "host_after_ns": after, "status": status,
                     "domain": "host" if before <= value <= after else
                         "controlled_virtual" if value in VALUES else "unknown"})
    return sorted(rows, key=lambda row: row["sequence"])


def _milliseconds(ns):
    # C++ duration_cast truncates toward zero, including a negative duration.
    return (ns // 1_000_000 if ns >= 0 else -((-ns) // 1_000_000)) % (1 << 64)


def _analyze(directory):
    samples = _samples(directory)
    raw = (directory / "heaptrack.raw").read_bytes()
    timestamps = {int(line.split()[1], 16) for line in raw.splitlines()
                  if line.startswith(b"c ")}
    # Keep the domain conclusion unknown unless a single observed origin value
    # explains EVERY retained c record using the source's timestamp arithmetic.
    candidates = [sample for sample in samples
        if timestamps and timestamps <= {
            _milliseconds(other["returned_ns"] - sample["returned_ns"])
            for other in samples}]
    domains = {sample["domain"] for sample in candidates}
    origin = next(iter(domains)) if len(domains) == 1 else "unknown"
    virtual = sorted({sample["returned_ns"] for sample in samples
                      if sample["domain"] == "controlled_virtual"})
    steady = [sample for sample in samples
              if sample["symbol"] and "steady_clock3now" in sample["symbol"]]
    result = {
        "audit_samples": samples,
        "audit_bound_reached": len(samples) >= 1024,
        "timestamp_count": sum(line.startswith(b"c ") for line in raw.splitlines()),
        "timestamp_values_ms": sorted(timestamps),
        "observed_clock_id": 1 if steady and len(steady) == len(samples) else None,
        "backend": "CLOCK_MONOTONIC via libstdc++ steady_clock::now" if
            steady and len(steady) == len(samples) else "unknown",
        "controlled_virtual_values_observed_ns": virtual,
        "origin_domain_inferred_from_all_timestamp_records": origin,
        "origin_candidates_ns": sorted({sample["returned_ns"] for sample in candidates}),
        "origin_inference": "observed clock values matched to every raw c record using signed duration_cast milliseconds and size_t conversion; not direct access to Heaptrack's private cached startTime",
        "host_mapping": None,
        "host_mapping_reason": "this audit identifies backend and origin domain; it does not establish a host-time mapping for arbitrary Run captures",
        "mapped_artifacts": {path: digest(Path(path)) for path in
            sorted({sample["module"] for sample in samples if sample["module"]})},
    }
    save(directory / "analysis.json", result)
    return result


def collect(out: Path, build: Path, tools) -> dict:
    out, build = out.resolve(), build.resolve()
    out.mkdir(parents=True, exist_ok=False)
    compiler = os.environ.get("CC", "cc")
    commands = [command([compiler, "-O2", "-g", "-Wall", "-Wextra",
        "-shared", "-fPIC", "-o", out / "clock_audit.so", HERE / "clock_audit.c", "-ldl"]),
        command([compiler, "-O2", "-g", "-Wall", "-Wextra", "-I", HERE.parent.parent / "include",
        "-o", out / "clock_probe", HERE / "clock_probe.c"])]
    assert all(row["exit_code"] == 0 for row in commands), commands
    report = {"host": {"machine": platform.machine(), "system": platform.platform()},
        "build_commands": commands, "tools": [command(["heaptrack", "--version"]),
        command([compiler, "--version"]), command(["dpkg-query", "-W", "heaptrack", "libstdc++6", "libc6"])],
        "artifacts": {str(path): digest(path) for path in
            (tools.preload, build / "libsil_clock_shim.so", out / "clock_audit.so", out / "clock_probe")},
        "bounds": {"raw_bytes": RAW_BOUND, "wall_seconds": WALL_BOUND,
                   "audit_records": 1024}, "runs": {}}
    for mode in ("unshimmed", "shimmed"):
        directory = out / mode
        directory.mkdir()
        region = directory / "clock.region"
        # reject sleep, allow profiler timer thread; t initially zero.
        region.write_bytes(struct.pack("<4Q", 0, 0, 1, 0))
        raw = directory / "heaptrack.raw"
        libraries = [out / "clock_audit.so", tools.preload]
        if mode == "shimmed":
            libraries.append(build / "libsil_clock_shim.so")
        env = {key: value for key, value in os.environ.items()
               if key not in {"LD_PRELOAD", "SIL_CLOCK_REGION"}}
        env.update({"LD_PRELOAD": ":".join(map(str, libraries)),
            "DUMP_HEAPTRACK_OUTPUT": str(raw),
            "SILPROOF_CLOCK_REGION": str(region),
            "SILPROOF_CLOCK_MAPS": str(directory / "maps.txt"),
            "SILPROOF_CLOCK_AUDIT": str(directory / "audit.bin")})
        if mode == "shimmed":
            env["SIL_CLOCK_REGION"] = str(region)
        began, stopped = time.monotonic(), None
        with (directory / "stdout.txt").open("wb") as stdout, \
                (directory / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen([out / "clock_probe"], env=env,
                                       stdout=stdout, stderr=stderr)
            while process.poll() is None:
                if time.monotonic() - began >= WALL_BOUND:
                    stopped = "wall_timeout"
                elif raw.exists() and raw.stat().st_size > RAW_BOUND:
                    stopped = "raw_size_limit"
                if stopped:
                    process.kill()
                    break
                time.sleep(0.005)
            process.wait()
        row = {"exit_code": process.returncode, "stopped": stopped,
               "wall_seconds": time.monotonic() - began}
        if process.returncode == 0 and not stopped:
            row.update(_analyze(directory))
        report["runs"][mode] = row
    report["complete"] = all(row["exit_code"] == 0 and not row["stopped"]
        and not row["audit_bound_reached"] for row in report["runs"].values())
    report["timestamp_clock_established_in_calibration"] = report["complete"] and all(
        row["observed_clock_id"] == 1 and
        row["origin_domain_inferred_from_all_timestamp_records"] in {"host", "controlled_virtual"}
        for row in report["runs"].values()) and (
        report["runs"]["shimmed"]["controlled_virtual_values_observed_ns"] == VALUES)
    report["status"] = ("established" if report["timestamp_clock_established_in_calibration"]
                        else "inconclusive")
    report["inventory"] = {str(path.relative_to(out)): digest(path)
        for path in sorted(out.rglob("*")) if path.is_file()}
    save(out / "results.json", report)
    return report


if __name__ == "__main__":
    from sil.profile_heap_run import find_heaptrack
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--build", required=True, type=Path)
    args = parser.parse_args()
    result = collect(args.out, args.build, find_heaptrack())
    assert result["status"] == "established", "clock calibration inconclusive; inspect results.json"
