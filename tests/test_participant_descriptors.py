"""A Process participant gets stdin, stdout and stderr from the runner, and no
other descriptor (#388).

The runner holds the Recording, the pipes of each participant that started
earlier, the mapped-region files and the diagnostics outputs while it starts a
participant. If one of them stays open across exec, a participant can write
into the Recording or into the protocol of another participant, and a child
that exits on stdin EOF stays alive while a sibling holds its stdin.
"""

import json
import os
import subprocess
import sys

from sil.manifest import Manifest, SubscriberRoute

from conftest import COMPAT_ROUTE_CAPACITY, ROOT
from test_run_boundary import ARRAY_SCHEMAS


def participant(script, *args):
    return [sys.executable, str(ROOT / "tests" / "participants" / script),
            *args]


def manifest(path, report):
    """Two Process participants. The second is a shimmed probe that reads an
    shm Channel, so the runner holds a pipe set, arenas and a clock region
    when it starts the probe. The runner starts participants in name order,
    so the names set which one starts second."""
    m = Manifest(duration_ns=30_000_000)
    m.add_schemas(ARRAY_SCHEMAS)
    m.add_channel("payload", schema="big.Payload", transport="shm")
    m.add_process(
        "a_source",
        command=participant("array_source.py"),
        step_period_ns=10_000_000,
        publishes=["payload"],
    )
    m.add_process(
        "b_probe",
        command=participant("descriptor_probe.py", str(report)),
        step_period_ns=10_000_000,
        subscribes=[SubscriberRoute("payload", capacity=COMPAT_ROUTE_CAPACITY)],
        shim=True,
    )
    return m.write(path).path


def test_the_second_participant_gets_no_runner_descriptor(run_sil, tmp_path):
    report = tmp_path / "descriptors.json"
    proc = run_sil(manifest(tmp_path / "m.json", report))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(report.read_text()) == {}


def environment_entries():
    return b"".join(f"{key}={value}".encode() + b"\0"
                    for key, value in os.environ.items())


def test_a_participant_of_a_diagnostics_run_gets_no_runner_descriptor(
        sil_diagnostics_launcher, sil_run_diagnostics, tmp_path):
    # The launcher path that `sil profile` uses, with the diagnostics outputs
    # open in the runner while it starts the participants.
    report = tmp_path / "descriptors.json"
    environment = tmp_path / "environment"
    environment.write_bytes(environment_entries())
    status_read, status_write = os.pipe()
    with environment.open("rb") as stream:
        proc = subprocess.run(
            [str(sil_diagnostics_launcher),
             "--status-fd", str(status_write),
             "--environment-fd", str(stream.fileno()),
             str(sil_run_diagnostics), str(manifest(tmp_path / "m.json", report)),
             "-o", str(tmp_path / "run.mcap"),
             "--diagnostics-out", str(tmp_path / "run.json"),
             "--activation-observations", str(tmp_path / "activations.jsonl")],
            capture_output=True, text=True, cwd=tmp_path,
            pass_fds=(status_write, stream.fileno()))
    os.close(status_write)
    with os.fdopen(status_read, "rb") as status:
        assert status.read() == b""
    assert proc.returncode == 0, proc.stderr
    assert json.loads(report.read_text()) == {}
