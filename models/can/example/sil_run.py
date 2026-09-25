"""Run the example through SiL: author the Manifest, Run it twice, keep the trace.

    python sil_run.py EXAMPLE.json SilCanBus.fmu WORKDIR [RUNNER]

RUNNER defaults to `sil-run` on PATH. Every Run has a deadline and a
Participant response deadline. The two Recordings of one Manifest must be
byte-identical. WORKDIR receives the Manifest, both Recordings, their logs
and `trace.json`. It states the SiL version, where `sil` was imported from,
and whether that module is a file of the installed `sil` distribution, so an
installed bundle can be told apart from a source tree.
"""

import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path

import sil
from sil import schema
from sil.manifest import Manifest, SubscriberRoute
from sil.recording import read_records

import configuration

HERE = Path(__file__).resolve().parent
BUS_PROFILE = "application/org.fmi-standard.fmi-ls-bus.can"
PARTICIPANT_TIMEOUT_MS = 5000
RUN_DEADLINE_S = 120


def buffer_schema(capacity):
    return {"can.Buffer": {"fields": [
        {"name": "data_length", "type": "u16"},
        {"name": "data", "type": "u8", "count": capacity},
        {"name": "data_event_time_ns", "type": "u64"},
    ]}}


def author(config, archive, path):
    """A Manifest whose command lines carry the whole configuration."""
    limits = configuration.packaged_limits(archive)
    inputs = configuration.requests(config, limits)
    nodes = range(1, len(config["nodes"]) + 1)
    requests = [f"in.node{n}" for n in nodes]
    observed = [f"out.node{n}" for n in nodes]
    manifest = Manifest(duration_ns=config["duration_ns"])
    manifest.add_schemas(buffer_schema(limits["max_binary_size"]))
    for channel in requests:
        # Latency 0 delivers each request in the Step its instant belongs to.
        manifest.add_channel(channel, schema="can.Buffer", latency_ns=0)
    for channel in observed:
        manifest.add_channel(channel, schema="can.Buffer")
    schedule = [[requests[node], instant, data.hex()] for instant, node, data in inputs]
    manifest.add_process(
        "nodes",
        command=["python", str(HERE / "stimulus.py"), json.dumps(schedule),
                 str(limits["max_binary_size"])],
        step_period_ns=config["step_period_ns"],
        publishes=requests,
    )
    command = ["python", "-m", "sil.fmi", "--instance", "bus", str(archive),
               "--bus-profile", BUS_PROFILE]
    for n in nodes:
        command += ["--bind", f"in.node{n}:data=bus.Node{n}.Rx_Data",
                    "--bind", f"out.node{n}:data=bus.Node{n}.Tx_Data"]
    for start in configuration.start_values(config):
        command += ["--start", start]
    manifest.add_process(
        "bus",
        command=command,
        step_period_ns=config["step_period_ns"],
        subscribes=[SubscriberRoute(channel, capacity=8) for channel in requests],
        publishes=observed,
        # After the nodes in every Slot, so a request reaches its own Step.
        priority=1,
    )
    return manifest.write(path)


def run(runner, manifest, recording):
    result = subprocess.run(
        [runner, str(manifest), "--participant-timeout-ms",
         str(PARTICIPANT_TIMEOUT_MS), "-o", str(recording)],
        capture_output=True, text=True, timeout=RUN_DEADLINE_S,
    )
    recording.with_suffix(".log").write_text(result.stdout + result.stderr)
    if result.returncode != 0:
        raise SystemExit(f"{runner} exited {result.returncode}; see "
                         f"{recording.with_suffix('.log')}")


def recorded_trace(recording, capacity):
    codec = schema.load(buffer_schema(capacity))["can.Buffer"]
    trace = []
    for channel, _, raw in read_records(recording):
        if channel.startswith("out.node"):
            fields = codec.unpack(raw)
            data = bytes(fields["data"][:fields["data_length"]])
            trace.append([fields["data_event_time_ns"],
                          int(channel.removeprefix("out.node")), data.hex()])
    return sorted(trace)


def imported_from_installed_distribution():
    """True if `sil` is a file the installed `sil` distribution records."""
    try:
        files = importlib.metadata.distribution("sil").files or []
    except importlib.metadata.PackageNotFoundError:
        return False
    module = Path(sil.__file__).resolve()
    return any(Path(file.locate()).resolve() == module for file in files)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(config_path, archive_path, workdir, runner="sil-run"):
    config = configuration.load(config_path)
    archive, workdir = Path(archive_path).resolve(), Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    manifest = author(config, archive, workdir / "manifest.json")
    recordings = [workdir / "run.mcap", workdir / "repeat.mcap"]
    for recording in recordings:
        run(runner, manifest.path, recording)
    if recordings[0].read_bytes() != recordings[1].read_bytes():
        raise SystemExit("two Runs of one Manifest produced different Recordings")
    build_info = subprocess.run([runner, "--build-info"], capture_output=True,
                                text=True, timeout=30, check=True).stdout
    capacity = configuration.packaged_limits(archive)["max_binary_size"]
    (workdir / "trace.json").write_text(json.dumps({
        "execution_path": "SiL runner and FMU group Importer",
        "sil_version": sil.__version__,
        "sil_module": sil.__file__,
        "sil_installed": imported_from_installed_distribution(),
        "runner_build_info": build_info,
        "fmu_sha256": sha256(archive),
        "manifest_sha256": manifest.hash,
        "recording_sha256": sha256(recordings[0]),
        "repeat_identical": True,
        "deadlines": {"run_s": RUN_DEADLINE_S,
                      "participant_response_ms": PARTICIPANT_TIMEOUT_MS},
        "trace": recorded_trace(recordings[0], capacity),
    }, indent=2) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:])
