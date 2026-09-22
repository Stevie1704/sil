"""Evidence gates must reject corrupted observations even under python -O."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cases import Case, InstanceCase, STEP_NS, STEPS
from qualify import check_recording


@pytest.mark.parametrize("records", [[], [("output0", 1, bytes(8))],
                                      [("output0", 0, bytes(8))] * 2])
def test_missing_mistimed_or_duplicate_samples_fail(monkeypatch, records):
    case = Case("AccController", (InstanceCase((42.5, 0.0, 25.0), {}),))
    monkeypatch.setattr("qualify.read_records", lambda path: records)
    with pytest.raises(RuntimeError):
        check_recording("bad", case, Path("unused.mcap"))


def test_one_instance_has_its_own_complete_sample_set(monkeypatch):
    case = Case("AccController", (InstanceCase((42.5, 0.0, 25.0), {}),))
    records = [("output0", step * STEP_NS, bytes(8)) for step in range(STEPS)]
    monkeypatch.setattr("qualify.read_records", lambda path: records)
    assert len(check_recording("one", case, Path("unused.mcap"))) == STEPS


def test_byte_gate_survives_optimization(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    script = "from pathlib import Path; from qualify import compare_files; import sys; compare_files(*map(Path, sys.argv[1:]))"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = subprocess.run([sys.executable, "-O", "-c", script, str(first), str(second)],
                            capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "byte mismatch" in result.stderr
