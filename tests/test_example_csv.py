"""The CSV replay example at the run boundary (issue #179).

A CSV file is converted into a Recording, the Replay participant publishes it
into a consumer, and the consumer republishes what it received. The expected
consumer view is stated by hand from `examples/csv/signals.csv` and
`mapping.json`; the example itself is imported, not restated.
"""

import importlib.util
from pathlib import Path

import pytest
from conftest import ROOT

from sil.csv_recording import convert
from sil.testing import run_simulation

EXAMPLE = ROOT / "examples" / "csv"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = _load("csv_manifest", EXAMPLE / "manifest.py")

# Channels numbered in name order by the observer.
GEAR, SPEED, RANGE = 0, 1, 2
MS = 1_000_000

# (observer Step, (publish time, Channel, value)). A Message is visible at the
# consumer's first Step after it was published, the default Latency.
CONSUMER_VIEW = [
    (10 * MS, {"published_ns": 0, "channel": SPEED, "value": 10.0}),
    (10 * MS, {"published_ns": 0, "channel": RANGE, "value": 90.0}),
    (10 * MS, {"published_ns": 0, "channel": GEAR, "value": 3.0}),
    (30 * MS, {"published_ns": 20 * MS, "channel": SPEED,
               "value": 10.050000190734863}),
    (30 * MS, {"published_ns": 20 * MS, "channel": GEAR, "value": 3.0}),
    # Two CSV rows share 40 ms; the consumer receives them in row order.
    (50 * MS, {"published_ns": 40 * MS, "channel": SPEED,
               "value": 10.100000381469727}),
    (50 * MS, {"published_ns": 40 * MS, "channel": RANGE, "value": 89.5}),
    (50 * MS, {"published_ns": 40 * MS, "channel": SPEED,
               "value": 10.149999618530273}),
    (70 * MS, {"published_ns": 60 * MS, "channel": SPEED,
               "value": 10.199999809265137}),
    (70 * MS, {"published_ns": 60 * MS, "channel": RANGE, "value": 89.0}),
    (70 * MS, {"published_ns": 60 * MS, "channel": GEAR, "value": 4.0}),
]


def converted(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    recording = directory / "signals.mcap"
    convert(EXAMPLE / "mapping.json", EXAMPLE / "signals.csv", recording)
    return recording


@pytest.fixture(scope="module")
def replay_result(sil_run, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("csv-replay")
    return run_simulation(
        manifest.csv_replay_manifest(converted(workdir)),
        runner=sil_run, workdir=workdir,
    )


def test_the_consumer_receives_every_message_exactly(replay_result):
    assert replay_result.messages("csv.seen") == CONSUMER_VIEW


def test_the_replayed_channels_carry_the_recorded_counts(replay_result):
    counts = {channel: len(replay_result.messages(channel))
              for channel in manifest.REPLAYED}
    assert counts == {"ego.speed": 5, "target.range": 3, "ego.gear": 3}


def test_conversion_and_run_repeat_byte_identically(sil_run, tmp_path):
    """Each attempt converts into the same path, so the two Manifests are the
    same bytes exactly when the two conversions are."""
    recordings, runs = [], []
    for attempt in ("first", "second"):
        recording = converted(tmp_path / "recording")
        recordings.append(recording.read_bytes())
        workdir = tmp_path / attempt
        workdir.mkdir()
        runs.append(run_simulation(manifest.csv_replay_manifest(recording),
                                   runner=sil_run, workdir=workdir))
    first, second = runs
    assert recordings[0] == recordings[1]
    assert first.manifest_hash == second.manifest_hash
    assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()
