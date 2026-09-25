"""The bundle verdict refuses a source-tree SiL, other archives and disagreement."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bundle_verdict  # noqa: E402

TRACE = [[111000, 1, "20000000"]]


def prepare(directory, *, installed=True, sil_trace=TRACE, independent_trace=TRACE,
            independent_fmu="abc"):
    (directory / "sil").mkdir()
    (directory / "sil/trace.json").write_text(json.dumps({
        "execution_path": "SiL", "sil_version": "0.1.0",
        "sil_module": "/any/layout/sil/__init__.py", "sil_installed": installed,
        "runner_build_info": "sil-run", "fmu_sha256": "abc",
        "manifest_sha256": "m", "recording_sha256": "r", "repeat_identical": True,
        "deadlines": {}, "trace": sil_trace,
    }))
    (directory / "independent.json").write_text(json.dumps({
        "execution_path": "FMPy", "reference_tool": {"fmpy": "0.3.32"},
        "fmu_sha256": independent_fmu, "trace": independent_trace,
    }))
    (directory / "runtime-image.txt").write_text("sha256:runtime\n")
    (directory / "qualification-image.txt").write_text("sha256:qualification\n")
    return directory


def test_equal_traces_from_the_installed_bundle_pass(tmp_path):
    result = bundle_verdict.verdict(prepare(tmp_path))
    assert result["failures"] == []
    assert result["traces_equal"]
    assert result["independent"]["reference_tool"] == {"fmpy": "0.3.32"}
    assert result["runtime_image"] == "sha256:runtime"


@pytest.mark.parametrize("changes, failure", [
    ({"installed": False}, "not an installed distribution"),
    ({"independent_fmu": "other"}, "different archives"),
    ({"independent_trace": []}, "disagree"),
])
def test_each_violation_is_reported(tmp_path, changes, failure):
    result = bundle_verdict.verdict(prepare(tmp_path, **changes))
    assert any(failure in message for message in result["failures"])
