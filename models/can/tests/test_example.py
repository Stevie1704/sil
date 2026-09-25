"""The documented example reproduces contention and a fault on two paths.

The hand-derived table below is checked against the independent FMPy master;
the SiL Run of the same configuration must record the same trace. Both write
their evidence to build/can/example/.
"""

import json
import struct
import sys
from pathlib import Path

import wire

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "models/can/example"
sys.path.insert(0, str(EXAMPLE))
import independent  # noqa: E402
import configuration  # noqa: E402

ARTIFACTS = ROOT / "build/can"
BUS = ARTIFACTS / "SilCanBus.fmu"
BIT_NS = 1_000_000_000 // 500_000


def frame(identifier, data):
    return struct.pack("<IIIBBH", 0x10, 16 + len(data), identifier, 0, 0, len(data)) + data


def confirm(identifier):
    return struct.pack("<III", 0x20, 12, identifier)


def lost(identifier):
    return struct.pack("<III", 0x30, 12, identifier)


def bus_error(identifier, primary):
    return struct.pack("<IIIBBB", 0x31, 15, identifier, 1, 1 if primary else 2,
                       1 if primary else 0)


def end(start, identifier, data):
    return start + wire.frame_bits(identifier, data) * BIT_NS


# Node2 (0x200) and Node3 (0x300, DiscardAndNotify) contend at 1 us; Node1's
# 0x100 at 500 us draws the scheduled Bit Error and its one bounded retry.
CONTENTION_END = end(1000, 0x200, b"\xaa")
ERROR_END = end(500_000, 0x100, b"\x01\x02")
RETRY_END = end(ERROR_END + wire.INTERMISSION * BIT_NS, 0x100, b"\x01\x02")
EXPECTED = sorted([
    [CONTENTION_END, 1, frame(0x200, b"\xaa").hex()],
    [CONTENTION_END, 2, confirm(0x200).hex()],
    [CONTENTION_END, 3, (lost(0x300) + frame(0x200, b"\xaa")).hex()],
    [ERROR_END, 1, bus_error(0x100, primary=True).hex()],
    [ERROR_END, 2, bus_error(0x100, primary=False).hex()],
    [ERROR_END, 3, bus_error(0x100, primary=False).hex()],
    [RETRY_END, 1, confirm(0x100).hex()],
    [RETRY_END, 2, frame(0x100, b"\x01\x02").hex()],
    [RETRY_END, 3, frame(0x100, b"\x01\x02").hex()],
])


def test_hand_derived_instants():
    assert (CONTENTION_END, ERROR_END, RETRY_END) == (111000, 628000, 762000)


def test_independent_master_reproduces_the_example():
    output = ARTIFACTS / "example/independent.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    independent.main(EXAMPLE / "example.json", BUS, output)
    assert json.loads(output.read_text())["trace"] == EXPECTED


def test_sil_run_matches_the_independent_master():
    import sil_run

    workdir = ARTIFACTS / "example/sil"
    sil_run.main(EXAMPLE / "example.json", BUS, workdir, "/opt/kernel/sil-run")
    evidence = json.loads((workdir / "trace.json").read_text())
    assert evidence["repeat_identical"]
    assert evidence["trace"] == EXPECTED


def test_other_step_period_and_node_count_need_no_source_edit(tmp_path):
    import sil_run

    config = configuration.load(EXAMPLE / "example.json")
    config["step_period_ns"] = 1000
    config["nodes"] = config["nodes"][:2]
    config["frames"] = [f for f in config["frames"] if f["node"] != 3]
    changed = tmp_path / "two-nodes.json"
    changed.write_text(json.dumps(config))
    expected = [row for row in EXPECTED if row[1] != 3]
    independent.main(changed, BUS, tmp_path / "independent.json")
    assert json.loads((tmp_path / "independent.json").read_text())["trace"] == expected
    sil_run.main(changed, BUS, tmp_path / "sil", "/opt/kernel/sil-run")
    assert json.loads((tmp_path / "sil/trace.json").read_text())["trace"] == expected
