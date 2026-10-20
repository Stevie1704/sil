"""Saved GDB/Activation evidence at the proof's offline command boundary."""
import json
from pathlib import Path
import subprocess
import sys

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
