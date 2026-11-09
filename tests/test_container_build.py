"""Build-stage APT recovery, without network access or a Docker daemon."""

import json
import os
import shlex
import shutil
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
         "install" if "--no-download" in args else "download")
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

    def run(script=None, build_args=None, **failures):
        command = (["sh", "-ec", script] if script is not None else
                   ["sh", str(ROOT / "container/install-snapshot-packages.sh"), "cmake", "libssl-dev"])
        result = subprocess.run(
            command,
            env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                 "APT_TEST_LOG": str(log), "APT_TEST_FAILURES": json.dumps(failures),
                 **(build_args or {})},
            capture_output=True, text=True, timeout=5,
        )
        return result, json.loads(log.read_text())

    return run


HELPER = "container/install-snapshot-packages.sh"
HELPER_COPY = f"COPY {HELPER} /tmp/install-snapshot-packages.sh"


def snapshot_installs():
    """Each RUN that configures snapshot sources, with its stage's state.

    Yields (dockerfile, ordinal, script, helper copied in this stage, ARG
    defaults). A helper copied in an earlier stage is not in this one.
    """
    dockerfiles = [ROOT / "Dockerfile", *sorted(
        path for root in ("proofs", "models", "worker")
        for path in (ROOT / root).rglob("Dockerfile*"))]
    for dockerfile in dockerfiles:
        instructions = dockerfile.read_text().replace("\\\n", " ").splitlines()
        build_args, helper_copied, ordinal = {}, False, 0
        for instruction in instructions:
            if instruction.startswith("FROM "):
                helper_copied = False
            elif instruction.startswith("ARG ") and "=" in instruction:
                name, value = instruction.removeprefix("ARG ").split("=", 1)
                build_args[name] = value
            elif instruction == HELPER_COPY:
                helper_copied = True
            elif instruction.startswith("RUN ") and "> /etc/apt/sources.list" in instruction:
                ordinal += 1
                yield (str(dockerfile.relative_to(ROOT)), ordinal,
                       instruction.removeprefix("RUN "), helper_copied, dict(build_args))


SNAPSHOT_INSTALLS = list(snapshot_installs())


def test_snapshot_installs_are_found():
    found = {(dockerfile, ordinal) for dockerfile, ordinal, *_ in SNAPSHOT_INSTALLS}
    assert {("Dockerfile", 1), ("proofs/osmp-qualification/Dockerfile", 2),
            ("proofs/libsafety/Dockerfile", 1)} <= found


@pytest.mark.parametrize(
    ("dockerfile", "ordinal", "script", "helper_copied", "build_args"),
    SNAPSHOT_INSTALLS, ids=[f"{d}#{n}" for d, n, *_ in SNAPSHOT_INSTALLS],
)
@pytest.mark.parametrize("phase", ["update", "download"])
def test_snapshot_build_recovers_at_the_dockerfile_call_site(
    apt_build, tmp_path, dockerfile, ordinal, script, helper_copied, build_args, phase,
):
    assert helper_copied, f"{dockerfile} stage does not copy {HELPER} before its install"
    helper = tmp_path / "install-snapshot-packages.sh"
    shutil.copyfile(ROOT / HELPER, helper)
    # Execute the Dockerfile's shell command with filesystem paths redirected
    # into the test workspace and failures injected only at the APT boundary.
    apt_dir = tmp_path / "apt"
    (apt_dir / "sources.list.d").mkdir(parents=True)
    script = script.replace("/etc/apt", shlex.quote(str(apt_dir)))
    script = script.replace("/tmp/install-snapshot-packages.sh", shlex.quote(str(helper)))
    script = script.replace("/var/lib/apt/lists", shlex.quote(str(tmp_path / "lists")))
    result, calls = apt_build(script=script, build_args=build_args, **{phase: 2})
    assert result.returncode == 0, result.stderr
    # Every APT call goes through the helper: no bare update or install.
    expected = {"update": 1, "download": 1, phase: 3}
    assert [call["phase"] for call in calls] == [
        *["update"] * expected["update"], *["download"] * expected["download"], "install"]
    assert not helper.exists()
    sources = (apt_dir / "sources.list").read_text()
    assert "http://snapshot.debian.org/archive/" in sources
    assert "/20260901T000000Z " in sources


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
