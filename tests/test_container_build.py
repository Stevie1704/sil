"""Build-stage APT recovery, without network access or a Docker daemon."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def apt_build(tmp_path):
    # Inject failures at the external APT boundary; execute the real helper.
    apt = tmp_path / "apt-get"
    apt.write_text(f"#!{sys.executable}\n" + '''
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["APT_TEST_LOG"])
calls = json.loads(log.read_text()) if log.exists() else []
args = sys.argv[1:]
phase = ("update" if "update" in args else
         "download" if "--download-only" in args else "install")
attempt = sum(call["phase"] == phase for call in calls)
calls.append({"phase": phase, "args": args})
log.write_text(json.dumps(calls))
failures = json.loads(os.environ["APT_TEST_FAILURES"])
if attempt < failures.get(phase, 0):
    print("E: Failed to fetch snapshot package: 503 first byte timeout", file=sys.stderr)
    sys.exit(100)
''')
    apt.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    log = tmp_path / "calls.json"

    def run(**failures):
        result = subprocess.run(
            ["sh", str(ROOT / "container/install-build-deps.sh"), "cmake", "libssl-dev"],
            env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                 "APT_TEST_LOG": str(log), "APT_TEST_FAILURES": json.dumps(failures)},
            capture_output=True, text=True, timeout=5,
        )
        return result, json.loads(log.read_text())

    return run


@pytest.mark.parametrize("phase", ["update", "download"])
def test_temporary_fetch_failures_recover_before_installing(apt_build, phase):
    result, calls = apt_build(**{phase: 2})
    assert result.returncode == 0, result.stderr
    assert sum(call["phase"] == phase for call in calls) == 3
    assert [call["phase"] for call in calls][-1] == "install"
    assert sum(call["phase"] == "install" for call in calls) == 1
    assert "--no-download" in calls[-1]["args"]


@pytest.mark.parametrize("phase", ["update", "download"])
def test_persistent_fetch_failure_stops_without_installing(apt_build, phase):
    result, calls = apt_build(**{phase: 99})
    assert result.returncode == 100
    assert sum(call["phase"] == phase for call in calls) == 3
    assert all(call["phase"] != "install" for call in calls)


def test_installation_failure_is_not_retried(apt_build):
    result, calls = apt_build(install=1)
    assert result.returncode == 100
    assert [call["phase"] for call in calls] == ["update", "download", "install"]


def test_fetches_use_timeouts_and_require_a_complete_index(apt_build):
    result, calls = apt_build()
    assert result.returncode == 0, result.stderr
    assert "--error-on=any" in calls[0]["args"]
    for call in calls[:-1]:
        assert "Acquire::http::Timeout=30" in call["args"]
        assert "Acquire::https::Timeout=30" in call["args"]
        assert "Acquire::Retries=0" in call["args"]
    for call in calls:
        assert call["args"][-2:] == ["cmake", "libssl-dev"] or call["phase"] == "update"
