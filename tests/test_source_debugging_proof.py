"""The proof's batch GDB workflow and offline saved-evidence boundary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "proofs/source-debugging/validate.py"


def saved_capture(path):
    # Independent worked example: outputs 5/6 belong to starts 40/50 ms,
    # while their Sample times describe the ends at 50/60 ms.
    stops = []
    observations = [{"runner_pid": 42, "runner_tid": 42, "clock": "host_monotonic"}]
    for index, virtual, sequence, sample, previous, limited, begin in (
            (4, 40_000_000, "5", "50000000", "-2", "-2.5", 100),
            (5, 50_000_000, "6", "60000000", "-2.5", "-3", 200)):
        observations.append({"kind": "activation", "participant": "hazard", "index": index,
            "virtual_time_ns": virtual, "begin_ns": str(begin), "end_ns": str(begin + 50),
            "outcome": "completed", "tid": 42})
        for stage, host in (("entry", begin + 10), ("decision", begin + 20), ("return", begin + 30)):
            values = {"previous_command": previous, "sequence": sequence, "sample_time_ns": sample,
                      "limited_result": limited, "selected": "12", "mode": "1", "target": "-3"}
            stops.append({"stage": stage, "hit": index, "virtual_time_ns": virtual,
                "host_monotonic_ns": host, "pid": 42, "ptid": [42, 42, 0],
                "values": {name: {"value": value, "reason": None} for name, value in values.items()}})
    (path / "stops.json").write_text(json.dumps(stops))
    (path / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in observations))
    return stops


def validate(path):
    return subprocess.run([sys.executable, str(VALIDATOR), str(path), "native"],
                          capture_output=True, text=True, timeout=10)


def test_saved_hazard_capture_maps_output_sequences_to_activation_starts(tmp_path):
    saved_capture(tmp_path)
    result = validate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "2 selected Activations verified" in result.stdout


def test_return_from_a_different_activation_is_rejected(tmp_path):
    stops = saved_capture(tmp_path)
    stops[2]["virtual_time_ns"] = 50_000_000
    (tmp_path / "stops.json").write_text(json.dumps(stops))
    result = validate(tmp_path)
    assert result.returncode != 0
    assert "Activation index / Virtual time mismatch" in result.stderr


def test_return_at_the_half_open_activation_end_is_rejected(tmp_path):
    stops = saved_capture(tmp_path)
    stops[2]["host_monotonic_ns"] = 150
    (tmp_path / "stops.json").write_text(json.dumps(stops))
    result = validate(tmp_path)
    assert result.returncode != 0
    assert "outside observed Activation" in result.stderr


def test_state_from_another_thread_cannot_complete_a_native_capture(tmp_path):
    stops = saved_capture(tmp_path)
    stops[2]["ptid"] = [42, 99, 0]
    (tmp_path / "stops.json").write_text(json.dumps(stops))
    result = validate(tmp_path)
    assert result.returncode != 0
    assert "thread ownership" in result.stderr


def test_three_fmus_share_one_stopped_group_process(tmp_path):
    stops = [{"stage": "entry", "instance": address, "pid": 77, "parent_pid": 66,
              "virtual_time_ns": 200_000_000, "hit": 2, "host_monotonic_ns": host,
              "all_threads_stopped": True, "state_unchanged_while_stopped": True,
              "children": "", "module": module}
             for address, host, module in (("0x1000", 110, "node1.so"),
                ("0x2000", 120, "node2.so"), ("0x3000", 130, "bus.so"))]
    (tmp_path / "stops.json").write_text(json.dumps(stops))
    rows = [{"runner_pid": 42}, {"kind": "spawn", "participant": "bus_group", "pid": 66},
            {"kind": "activation", "participant": "bus_group", "index": 2,
             "virtual_time_ns": 200_000_000, "begin_ns": "100", "end_ns": "150"}]
    (tmp_path / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = subprocess.run([sys.executable, str(VALIDATOR), str(tmp_path), "bus-group"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "3 grouped FMU instances verified" in result.stdout


def test_output_id_cannot_substitute_for_wrong_live_lead_state(tmp_path):
    saved_capture(tmp_path)
    probes = []
    for index, x in ((4, "999"), (5, "28.75")):
        probes.append({"hit": index, "values": {name: {"value": value, "reason": None}
            for name, value in {"selected": "12", "lead.x_m": x, "lead.relative_vx_mps": "-25"}.items()},
            "inline_values": {"closing_mps": {"value": "25", "reason": None}}})
    (tmp_path / "selection-stops.json").write_text(json.dumps(probes))
    result = validate(tmp_path)
    assert result.returncode != 0
    assert "live selected lead" in result.stderr


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("gdb") or not shutil.which("cc"),
                    reason="batch GDB cleanup requires Linux, GDB and a C compiler")
def test_batch_detach_reaps_inferior_before_debugger_returns(tmp_path):
    source = tmp_path / "controller.c"
    source.write_text("""
#define _POSIX_C_SOURCE 200809L
#include "adas_reference.h"
#include <time.h>
int main(void) {
    adas_ref_instance instance;
    adas_ref_config config = {10000000, -3, 0.5};
    adas_ref_output output;
    adas_ref_fault fault;
    adas_ref_init(&instance, &config, &fault);
    for (int i = 0; i < 6; ++i)
        adas_ref_advance(&instance, i * 10000000, &output, &fault);
    struct timespec pause = {1, 0};
    nanosleep(&pause, 0);
    return 0;
}
""")
    example = ROOT / "examples/adas-reference"
    binary = tmp_path / "controller"
    subprocess.run(["cc", "-std=c11", "-O2", "-g", "-ffp-contract=off", "-I", str(example),
                    str(source), str(example / "adas_reference.c"), "-lm", "-o", str(binary)],
                   check=True, capture_output=True, timeout=30)
    env = {**os.environ, "SIL_DEBUG_DEST": str(tmp_path), "SIL_DEBUG_SELECTOR": "index",
           "SIL_DEBUG_ACTION": "detach"}
    env.pop("SIL_DEBUG_SHIM", None)
    # Own orphaned children so init's reaping speed cannot mask this regression.
    # The supervisor always reaps them, including when the assertion fails.
    supervisor = """
import ctypes, json, os, pathlib, subprocess, sys
PR_SET_CHILD_SUBREAPER = 36
assert ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
dest = pathlib.Path(sys.argv[1])
try:
    with (dest / 'gdb.txt').open('w') as log:
        result = subprocess.run(sys.argv[2:], stdout=log, stderr=subprocess.STDOUT, timeout=10)
    assert result.returncode == 0, (dest / 'gdb.txt').read_text()
    pid = json.loads((dest / 'action.json').read_text())['inferior_pid']
    assert not pathlib.Path(f'/proc/{pid}').exists(), 'detached inferior survived debugger exit'
finally:
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            break
"""
    result = subprocess.run([sys.executable, "-c", supervisor, str(tmp_path),
        "gdb", "-nx", "-q", "-batch", "-x", str(ROOT / "proofs/source-debugging/session.py"),
        "--args", str(binary)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
