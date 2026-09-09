#!/usr/bin/env python3
"""Baseline for the current copied large-Message routing path (issue #61).

Runs one matrix of Manifests twice per configuration: once through
`sil-run-instrumented`, which reports how many payload copies the routing path
actually made and what the kernel process itself cost, and `--repeats` times
through the production `sil-run` for wall-clock. Copy counts therefore never
come from timing, and timing never comes from a build carrying counters.

Dimensions swept: payload size, subscriber fan-out, Recording on or off,
Native versus Process participants, inline versus shared-memory Transport, and
single-Message versus burst delivery.

    tools/bench_routing.py --build-dir build

Writes `routing-baseline.json` (raw) and `routing-baseline.md` (tables) into
docs/bench/. The narrative and the go/no-go inputs live alongside them in
docs/bench/large-message-routing-baseline.md.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from sil import schema as sil_schema  # noqa: E402
from sil.manifest import Manifest  # noqa: E402

BENCH_SCHEMAS = json.loads((ROOT / "schemas" / "bench.json").read_text())
_TYPES = sil_schema.load(BENCH_SCHEMAS)

# One activation period for every participant in the matrix, so a row's cost
# scales with its message count rather than with its schedule.
PERIOD_NS = 10_000_000

# The two payloads the baseline is stated over: a small control message and one
# representative sensor frame. Their byte layout comes from schemas/bench.json.
PAYLOADS = {
    "small": "bench.Small",
    "camera": "bench.CameraFrame",
}

COPY_SITES = [
    "caller_to_kernel",
    "subscriber_copy",
    "recorded",
    "arena_write",
    "arena_read",
    "inline_encode",
    "inline_decode",
]


def payload_bytes(payload: str) -> int:
    return _TYPES[PAYLOADS[payload]].size


@dataclass
class Config:
    """One row of the matrix: a Manifest plus the dimensions it stands for."""

    name: str
    dimensions: dict
    manifest: Manifest
    messages: int
    recording: bool


# --- manifest shapes ---------------------------------------------------------


def _base(payload: str, messages: int, burst: int, transport: str) -> Manifest:
    schema_name = PAYLOADS[payload]
    m = Manifest(duration_ns=(messages // burst) * PERIOD_NS)
    m.add_schemas({schema_name: BENCH_SCHEMAS[schema_name]})
    m.add_channel("frames", schema=schema_name, transport=transport)
    return m


def _native_source(m: Manifest, build_dir: Path, payload: str, burst: int) -> None:
    m.add_native(
        "source",
        library=str(build_dir / "bench_source.silp"),
        config={"channel": "frames", "bytes": payload_bytes(payload),
                "period_ns": PERIOD_NS, "burst": burst},
        publishes=["frames"],
    )


def _native_sink(m: Manifest, build_dir: Path, name: str) -> None:
    m.add_native(
        name,
        library=str(build_dir / "bench_sink.silp"),
        config={"input": "frames", "period_ns": PERIOD_NS},
        subscribes=["frames"],
    )


def _process(m: Manifest, name: str, shape: str, burst: int, **channels) -> None:
    command = [sys.executable, str(ROOT / "tools" / "bench_participants.py"), shape]
    if shape == "source":
        command.append(str(burst))
    m.add_process(name, command=command, step_period_ns=PERIOD_NS,
                  priority=1, **channels)


def native_config(build_dir, payload, subscribers, recording, messages) -> Config:
    """Native fan-out: one publisher, `subscribers` in-process subscribers."""
    m = _base(payload, messages, burst=1, transport="inline")
    _native_source(m, build_dir, payload, burst=1)
    for i in range(subscribers):
        _native_sink(m, build_dir, f"sink{i}")
    return Config(
        name=f"native-{payload}-fanout{subscribers}-rec{'on' if recording else 'off'}",
        dimensions={"participants": "native", "payload": payload,
                    "subscribers": subscribers, "transport": "n/a",
                    "burst": 1, "recording": recording},
        manifest=m, messages=messages, recording=recording,
    )


def process_config(build_dir, payload, direction, transport, burst, recording,
                   messages) -> Config:
    """Process delivery in one direction, over one Transport, at one burst size.

    `direction` names which side the payload crosses the boundary towards:
    "in" is kernel to Process participant, "out" is Process participant to
    kernel. A burst larger than one puts several Messages on one Channel in one
    step, which is what makes the single-slot Arena fall back to inline.
    """
    m = _base(payload, messages, burst, transport)
    if direction == "in":
        _native_source(m, build_dir, payload, burst)
        _process(m, "sink", "sink", burst, subscribes=["frames"])
    else:
        _process(m, "source", "source", burst, publishes=["frames"])
        _native_sink(m, build_dir, "sink0")
    return Config(
        name=f"process-{payload}-{direction}-{transport}-burst{burst}"
             f"-rec{'on' if recording else 'off'}",
        dimensions={"participants": f"process-{direction}", "payload": payload,
                    "subscribers": 1, "transport": transport,
                    "burst": burst, "recording": recording},
        manifest=m, messages=messages, recording=recording,
    )


def matrix(build_dir: Path, counts: dict[str, int]) -> list[Config]:
    # A camera frame run writes its payload size times its message count to
    # disk when Recording is on, so its rows carry fewer messages than the
    # small-payload rows. Every figure is reported per message, and the count
    # is stated per row, so the two remain comparable.
    configs = [
        # Fixed cost of a run, so every other row can be read net of it.
        native_config(build_dir, "small", 1, True, 1),
    ]
    # Native fan-out at both payload sizes, with and without Recording: this is
    # where a per-subscriber payload copy would show up.
    for payload, messages in counts.items():
        for subscribers in (0, 1, 2, 4, 8):
            for recording in (True, False):
                configs.append(
                    native_config(build_dir, payload, subscribers, recording,
                                  messages))
    # Process delivery: both directions, inline against the single-slot Arena,
    # single Message against a burst that forces the inline fallback.
    for payload, messages in counts.items():
        for direction in ("in", "out"):
            for transport in ("inline", "shm"):
                for burst in (1, 2):
                    configs.append(
                        process_config(build_dir, payload, direction, transport,
                                       burst, True, messages))
    # Recording-off counterparts for the shared-memory rows, so Recording I/O
    # can be separated from transport cost on the Process path too.
    for payload, messages in counts.items():
        for direction in ("in", "out"):
            configs.append(
                process_config(build_dir, payload, direction, "shm", 1, False,
                               messages))
    return configs


# --- execution ---------------------------------------------------------------


def _args(runner: Path, manifest_path: Path, out: Path, recording: bool):
    args = [str(runner), str(manifest_path)]
    return args + (["-o", str(out)] if recording else ["--no-recording"])


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def run_timed(runner: Path, manifest_path: Path, workdir: Path, recording: bool):
    """One production run: wall-clock, and CPU for the whole process tree.

    RUSAGE_CHILDREN accumulates monotonically and this driver runs one child at
    a time, so the difference across a run is exactly that run's CPU — kernel
    and any Process participants together.
    """
    out = workdir / "out.mcap"
    before = _cpu_seconds()
    start = time.perf_counter()
    proc = subprocess.run(_args(runner, manifest_path, out, recording),
                          capture_output=True, text=True, cwd=workdir)
    wall = time.perf_counter() - start
    cpu = _cpu_seconds() - before
    if proc.returncode != 0:
        raise SystemExit(f"run failed ({proc.returncode}): {proc.stderr.strip()}")
    out.unlink(missing_ok=True)
    return wall, cpu


def run_instrumented(runner: Path, manifest_path: Path, workdir: Path,
                     recording: bool) -> dict:
    """One instrumented run: payload-copy counts and the kernel's own cost."""
    report = workdir / "counters.json"
    env = dict(os.environ, SIL_COPY_COUNTERS_OUT=str(report))
    out = workdir / "out.mcap"
    proc = subprocess.run(_args(runner, manifest_path, out, recording),
                          capture_output=True, text=True, cwd=workdir, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"run failed ({proc.returncode}): {proc.stderr.strip()}")
    out.unlink(missing_ok=True)
    return json.loads(report.read_text())


def measure(config: Config, build_dir: Path, repeats: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        ref = config.manifest.write(workdir / "manifest.json")
        counters = run_instrumented(build_dir / "sil-run-instrumented", ref.path,
                                    workdir, config.recording)
        samples = [run_timed(build_dir / "sil-run", ref.path, workdir,
                             config.recording) for _ in range(repeats)]

    walls = sorted(s[0] for s in samples)
    cpus = sorted(s[1] for s in samples)
    payload = config.dimensions["payload"]
    copies = {site: counters[site] for site in COPY_SITES}
    copied_bytes = sum(copies[site]["bytes"] for site in COPY_SITES
                       if site != "recorded")
    return {
        "name": config.name,
        "dimensions": config.dimensions,
        "messages": config.messages,
        "payload_bytes": payload_bytes(payload),
        "manifest_hash": ref.hash,
        "wall_s": {"median": statistics.median(walls), "min": walls[0],
                   "max": walls[-1], "repeats": repeats},
        "tree_cpu_s": {"median": statistics.median(cpus), "min": cpus[0],
                       "max": cpus[-1]},
        "kernel_user_s": counters["kernel_user_s"],
        "kernel_system_s": counters["kernel_system_s"],
        "kernel_max_rss_bytes": counters["kernel_max_rss_bytes"],
        "copies": copies,
        "copied_bytes_per_message": copied_bytes / config.messages,
        "us_per_message": statistics.median(walls) / config.messages * 1e6,
        "payload_mib_per_s": (payload_bytes(payload) * config.messages)
        / statistics.median(walls) / (1 << 20),
    }


# --- reporting ---------------------------------------------------------------


def _mib(value: float) -> str:
    return f"{value / (1 << 20):.1f}"


def _copy_cell(result: dict, site: str) -> str:
    return str(result["copies"][site]["count"])


def _kernel_cpu(result: dict) -> float:
    return result["kernel_user_s"] + result["kernel_system_s"]


def _native_table(results: list[dict]) -> list[str]:
    lines = [
        "| payload | messages | subscribers | recording | run ms | "
        "µs/message | MiB/s | kernel CPU s | kernel RSS MiB | caller copies | "
        "subscriber copies | recorded |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        d = r["dimensions"]
        lines.append(
            f"| {d['payload']} | {r['messages']} | {d['subscribers']} | "
            f"{'on' if d['recording'] else 'off'} | "
            f"{r['wall_s']['median'] * 1e3:.1f} | {r['us_per_message']:.1f} | "
            f"{r['payload_mib_per_s']:.1f} | {_kernel_cpu(r):.3f} | "
            f"{_mib(r['kernel_max_rss_bytes'])} | "
            f"{_copy_cell(r, 'caller_to_kernel')} | "
            f"{_copy_cell(r, 'subscriber_copy')} | {_copy_cell(r, 'recorded')} |"
        )
    return lines


def _process_dimensions(result: dict) -> str:
    d = result["dimensions"]
    return (f"| {d['payload']} | {d['participants'].removeprefix('process-')} | "
            f"{d['transport']} | {d['burst']} | "
            f"{'on' if d['recording'] else 'off'} ")


def _process_cost_table(results: list[dict]) -> list[str]:
    lines = [
        "| payload | direction | transport | burst | recording | messages | "
        "µs/message | MiB/s | kernel CPU s | tree CPU s | kernel RSS MiB |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            _process_dimensions(r)
            + f"| {r['messages']} | {r['us_per_message']:.1f} | "
            f"{r['payload_mib_per_s']:.1f} | {_kernel_cpu(r):.3f} | "
            f"{r['tree_cpu_s']['median']:.3f} | "
            f"{_mib(r['kernel_max_rss_bytes'])} |"
        )
    return lines


def _process_copy_table(results: list[dict]) -> list[str]:
    """The Process rows' copy counts, kernel-side sites first.

    A Process publication is copied out of the Arena or the base64 line into a
    kernel buffer and then copied again by the publish path, so the two
    kernel-side columns are the ones that decide whether #58 has a payload-copy
    case at all.
    """
    lines = [
        "| payload | direction | transport | burst | recording | caller | "
        "subscriber | arena write | arena read | inline encode | "
        "inline decode |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            _process_dimensions(r)
            + f"| {_copy_cell(r, 'caller_to_kernel')} | "
            f"{_copy_cell(r, 'subscriber_copy')} | "
            f"{_copy_cell(r, 'arena_write')} | {_copy_cell(r, 'arena_read')} | "
            f"{_copy_cell(r, 'inline_encode')} | {_copy_cell(r, 'inline_decode')} |"
        )
    return lines


def render_markdown(report: dict) -> str:
    env = report["environment"]
    native = [r for r in report["results"]
              if r["dimensions"]["participants"] == "native"]
    process = [r for r in report["results"]
               if r["dimensions"]["participants"].startswith("process")]
    control = next((r for r in native if r["messages"] == 1), None)
    native = [r for r in native if r is not control]
    lines = [
        "# Routing baseline — raw results",
        "",
        "Generated by `tools/bench_routing.py`; do not edit by hand.",
        "The procedure, the reading, and the go/no-go inputs are in",
        "[large-message-routing-baseline.md](large-message-routing-baseline.md).",
        "",
        f"- machine: {env['platform']}",
        "- messages per run: "
        + ", ".join(f"{name} {count}" for name, count in
                    report["messages"].items()),
        f"- publish period: {report['period_ns'] / 1e6:.0f} ms "
        f"({1e9 / report['period_ns']:.0f} Hz)",
        f"- wall-clock repeats per row: {report['repeats']} (median reported)",
        f"- payload sizes: "
        + ", ".join(f"{name} {payload_bytes(name)} B" for name in PAYLOADS),
        "",
        "Copy columns are counts reported by the instrumented kernel, not",
        "figures inferred from timing.",
        "",
    ]
    if control:
        lines += [
            "## Fixed cost control",
            "",
            f"A one-Message run of the same shape costs "
            f"{control['wall_s']['median'] * 1e3:.1f} ms wall-clock; every "
            "µs/message figure below still carries that fixed cost once.",
            "",
        ]
    lines += [
        "## Native fan-out",
        "",
        *_native_table(native),
        "",
        "## Process transport",
        "",
        *_process_cost_table(process),
        "",
        "### Process transport — copy counts",
        "",
        "Counts on an input Channel are of delivered Messages: the last",
        "activation's inputs become visible after the Run ends, so a row shows",
        "`messages - burst` crossings rather than `messages`.",
        "",
        *_process_copy_table(process),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "bench")
    parser.add_argument("--messages", type=int, default=200,
                        help="small-payload messages per run (default: 200)")
    parser.add_argument("--camera-messages", type=int, default=50,
                        help="camera-frame messages per run (default: 50)")
    parser.add_argument("--repeats", type=int, default=5,
                        help="production runs timed per row (default: 5)")
    parser.add_argument("--only", default="",
                        help="run only rows whose name contains this substring")
    args = parser.parse_args()

    for exe in ("sil-run", "sil-run-instrumented"):
        if not (args.build_dir / exe).exists():
            raise SystemExit(f"{exe} not built in {args.build_dir}")

    counts = {"small": args.messages, "camera": args.camera_messages}
    configs = [c for c in matrix(args.build_dir, counts) if args.only in c.name]
    results = []
    for index, config in enumerate(configs, 1):
        print(f"[{index}/{len(configs)}] {config.name}", file=sys.stderr,
              flush=True)
        results.append(measure(config, args.build_dir, args.repeats))

    report = {
        "messages": counts,
        "repeats": args.repeats,
        "period_ns": PERIOD_NS,
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "results": results,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "routing-baseline.json").write_text(
        json.dumps(report, indent=2) + "\n")
    (args.out_dir / "routing-baseline.md").write_text(render_markdown(report))
    print(f"wrote {args.out_dir / 'routing-baseline.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
