"""Workload and Manifests of the profiling qualification (#319).

`build` compiles every binary with one compiler and `-O2 -g`:

- the #314 workload (proofs/profiling/workload.py): a stripped Native
  library with a matching and a mismatched separate debug file, a shimmed
  Process participant that loads a library with ctypes and starts
  short-lived children, and an FMU that the FMI Importer extracts into the
  Run working directory;
- the CAN stand-in FMUs of tests/fixtures/fmi_clock.c and fmi_bus.c,
  packaged as tests/can_fixture.py packages them, for an FMU group.

It writes `artifacts.json`. `manifests` authors the Runs from it with the
Python that runs this file, so the Manifests name the installed `sil`:

| Manifest | Content |
| --- | --- |
| `main` | the #314 Run: Native, shimmed Process + children, single FMU; 10 ms |
| `mixed` | Replay of `fmu.In` (10 ms) into the FMU, an FMU group of two CAN nodes and a bus + observer (20 ms), the probe (30 ms) |
| `failure` | `mixed`, the probe fails at Step 7 |
| `deadline` | `mixed`, the probe sleeps 3 s at Step 3 (run with a 1 s deadline) |
| `long` | `mixed` over 3 s, the probe sleeps 40 ms in each Step (to interrupt) |

`mixed` replays the Recording of an ordinary Run of `main`, so `manifests`
needs that Recording.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

from sil.manifest import Manifest, SubscriberRoute

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "proofs" / "profiling"))
import workload  # noqa: E402  the #314 workload

MS = 1_000_000
PROBE = HERE / "probe_participant.py"
OBSERVER = ROOT / "tests" / "participants" / "can_observer.py"
CAN_PROFILE = "application/org.fmi-standard.fmi-ls-bus.can"
CAN_SCHEMA = "can.Buffer"
CAN_BUFFER_BYTES = 2048
CAN_CHANNELS = {"can.node1.Tx": "node1.CanChannel.Tx_Data",
                "can.node2.Tx": "node2.CanChannel.Tx_Data",
                "can.bus.Node1": "bus.Node1.Tx_Data",
                "can.bus.Node2": "bus.Node2.Tx_Data"}
PERIODS_NS = {"fmu": 10 * MS, "group": 20 * MS, "observer": 20 * MS,
              "probe": 30 * MS}


def _can_fixture():
    # tests/can_fixture.py reads ROOT from the test conftest only.
    sys.modules.setdefault("conftest", types.SimpleNamespace(ROOT=ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    import can_fixture
    return can_fixture


def _cc_shared(out: Path, source: Path, *defines: str) -> Path:
    subprocess.run([shutil.which("cc") or "cc", *workload.CFLAGS, "-shared",
                    "-fPIC", *defines, "-o", str(out), str(source)],
                   check=True)
    return out


def build(out: Path) -> dict:
    """Compile and package everything into `out`; write artifacts.json."""
    out.mkdir(parents=True, exist_ok=False)
    built = workload.build(out / "main")
    can = _can_fixture()
    from sil.fmi import library_suffix, platform_directory
    suffix = library_suffix()
    fixtures = ROOT / "tests" / "fixtures"
    (out / "can").mkdir()
    node = _cc_shared(out / "can" / f"ClockedCanNodeOnABus{suffix}",
                      fixtures / "fmi_clock.c", "-DFMI_CLOCK_ECHO_RX=0")
    bus = _cc_shared(out / "can" / f"ClockedCanBus{suffix}",
                     fixtures / "fmi_bus.c")
    packaging = {"platform_directory": platform_directory()}
    artifacts = {
        **{k: str(v) for k, v in dataclasses.asdict(built).items()},
        "can_node_fmu": str(can.node_fmu(
            out / "can" / "node.fmu", model_identifier="ClockedCanNodeOnABus",
            binary=node, **packaging)),
        "can_bus_fmu": str(can.bus_fmu(
            out / "can" / "bus.fmu", model_identifier="ClockedCanBus",
            binary=bus, **packaging)),
    }
    (out / "artifacts.json").write_text(json.dumps(artifacts, indent=2)
                                        + "\n")
    return artifacts


def load(out: Path) -> tuple[workload.Artifacts, dict]:
    raw = json.loads((out / "artifacts.json").read_text())
    fields = {f.name for f in dataclasses.fields(workload.Artifacts)}
    built = workload.Artifacts(**{
        k: (Path(v) if v not in (None, "None") else None)
        for k, v in raw.items() if k in fields})
    return built, raw


def main_manifest(built: workload.Artifacts) -> Manifest:
    return workload.manifest(built, python=sys.executable)


def _mixed(raw: dict, reference: Path, probe_arguments: list[str],
           duration_ns: int = 1000 * MS) -> Manifest:
    m = Manifest(duration_ns=duration_ns)
    m.add_schemas({k: v for k, v in workload.SCHEMAS.items()
                   if k.startswith("fmu.")})
    m.add_schemas({
        "probe.Out": {"fields": [
            {"name": "step", "type": "u64"},
            {"name": "argv_digest", "type": "u64"},
            {"name": "started_in_command_directory", "type": "u64"},
            {"name": "in_run_directory", "type": "u64"},
            {"name": "env_digest", "type": "u64"},
            {"name": "work", "type": "u64"}]},
        CAN_SCHEMA: {"fields": [
            {"name": "data_length", "type": "u16"},
            {"name": "data", "type": "u8", "count": CAN_BUFFER_BYTES},
            {"name": "data_event_time_ns", "type": "u64"}]}})
    m.add_channel("fmu.In", schema="fmu.In")
    m.add_channel("fmu.Out", schema="fmu.Out")
    m.add_channel("probe.out", schema="probe.Out")
    for channel in CAN_CHANNELS:
        m.add_channel(channel, schema=CAN_SCHEMA)
    m.add_replay("replay", recording=str(reference), channels=["fmu.In"])
    m.add_process(
        "fmu", command=[sys.executable, "-m", "sil.fmi", raw["fmu"]],
        step_period_ns=PERIODS_NS["fmu"],
        subscribes=[SubscriberRoute("fmu.In", capacity=2)],
        publishes=["fmu.Out"], priority=1)
    m.add_process(
        "group", command=[
            sys.executable, "-m", "sil.fmi",
            "--instance", "node1", raw["can_node_fmu"],
            "--instance", "node2", raw["can_node_fmu"],
            "--instance", "bus", raw["can_bus_fmu"],
            "--bus-profile", CAN_PROFILE,
            "--connect", "node1.CanChannel=bus.Node1",
            "--connect", "node2.CanChannel=bus.Node2",
            *(argument for channel, variable in CAN_CHANNELS.items()
              for argument in ("--bind", f"{channel}:data={variable}")),
            "--start", "bus.BusErrorProbability=0.0"],
        step_period_ns=PERIODS_NS["group"], publishes=list(CAN_CHANNELS),
        priority=1)
    m.add_process(
        "observer", command=[sys.executable, str(OBSERVER)],
        step_period_ns=PERIODS_NS["observer"],
        subscribes=[SubscriberRoute(c, capacity=8) for c in CAN_CHANNELS],
        priority=2)
    m.add_process(
        "probe", command=[sys.executable, str(PROBE), *probe_arguments],
        step_period_ns=PERIODS_NS["probe"], publishes=["probe.out"])
    return m


def manifests(built: workload.Artifacts, raw: dict, reference: Path,
              out: Path) -> dict[str, Path]:
    """Write every Manifest into `out`; `reference` is an ordinary
    Recording of `main`."""
    out.mkdir(parents=True, exist_ok=True)
    documents = {
        "main": main_manifest(built),
        "mixed": _mixed(raw, reference, []),
        "failure": _mixed(raw, reference, ["--fail-at-step", "7"]),
        "deadline": _mixed(raw, reference, ["--sleep-at-step", "3",
                                            "--sleep-ms", "3000"]),
        "long": _mixed(raw, reference, ["--sleep-every-step-ms", "40"],
                       duration_ns=3000 * MS),
    }
    paths = {}
    for name, document in documents.items():
        paths[name] = out / f"{name}.json"
        document.write(str(paths[name]))
    return paths


def main_only(built: workload.Artifacts, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "main.json"
    main_manifest(built).write(str(path))
    return path
