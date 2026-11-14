"""Per-Channel record mode (#398): what a Run's Recording stores for each
Message of a `full`, `digest` or `off` Channel, and how each Recording tool and
Replay treats a Channel that holds no payload."""

import hashlib
import json
import sys

import pytest
from mcap.reader import make_reader

from conftest import ROOT, run_manifest
from toys import add_producer, toy_manifest

TICK_NS = 10_000_000
DURATION_NS = 50_000_000
DIGEST_SCHEMA = {"fields": [{"name": "sha256", "type": "u8", "count": 32}]}


def producer_manifest(record: str | None):
    """One Native producer on 'ticks', plus a second full Channel 'echo'."""
    m = toy_manifest(duration_ns=DURATION_NS)
    if record is None:
        m.add_channel("ticks", schema="toy.Counter")
    else:
        m.add_channel("ticks", schema="toy.Counter", record=record)
    m.add_channel("echo", schema="toy.Counter")
    add_producer(m, "producer", channel="ticks", period_ns=TICK_NS)
    m.add_process(
        "source",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "counter_source.py"),
                 "echo"],
        step_period_ns=TICK_NS,
        publishes=["echo"],
    )
    return m


def run(sil_run, manifest, path):
    ref = manifest.write(path.with_suffix(".json"))
    proc = run_manifest(sil_run, ref.path, path)
    assert proc.returncode == 0, proc.stderr
    return path


def mcap(path):
    """(channels by topic, schemas by id, [(topic, log_time, data)])."""
    with open(path, "rb") as f:
        reader = make_reader(f)
        summary = reader.get_summary()
        messages = [(channel.topic, message.log_time, message.data)
                    for _, channel, message in reader.iter_messages(
                        log_time_order=False)]
    channels = {c.topic: c for c in summary.channels.values()}
    return channels, summary.schemas, messages


@pytest.fixture(scope="module")
def recordings(sil_run, tmp_path_factory):
    """One Run of the same Manifest in each record mode."""
    directory = tmp_path_factory.mktemp("record-mode")
    return {mode: run(sil_run, producer_manifest(mode), directory / f"{mode}.mcap")
            for mode in ("full", "digest", "off")}


def test_explicit_full_writes_the_same_recording_bytes_as_no_record_key(
        sil_run, tmp_path, recordings):
    implicit = run(sil_run, producer_manifest(None), tmp_path / "implicit.mcap")

    assert implicit.read_bytes() == recordings["full"].read_bytes()
    channels, _, _ = mcap(implicit)
    assert all(channel.metadata == {} for channel in channels.values())


def test_a_raw_manifest_with_explicit_full_runs_like_one_without_it(
        sil_run, tmp_path, recordings):
    doc = producer_manifest(None).to_doc()
    doc["channels"]["ticks"]["record"] = "full"
    path = tmp_path / "explicit.json"
    path.write_text(json.dumps(doc))

    proc = run_manifest(sil_run, path, tmp_path / "explicit.mcap")

    assert proc.returncode == 0, proc.stderr
    _, _, full = mcap(recordings["full"])
    _, _, explicit = mcap(tmp_path / "explicit.mcap")
    assert explicit == full


def test_digest_writes_the_sha256_of_each_payload_with_a_digest_schema(
        recordings):
    _, _, full = mcap(recordings["full"])
    channels, schemas, digest = mcap(recordings["digest"])

    expected = [(topic, t, hashlib.sha256(data).digest() if topic == "ticks"
                 else data) for topic, t, data in full]
    assert digest == expected
    ticks = channels["ticks"]
    assert ticks.metadata == {"sil_record": "digest",
                              "sil_schema": "toy.Counter"}
    assert schemas[ticks.schema_id].name == "sil.Sha256Digest"
    assert json.loads(schemas[ticks.schema_id].data) == DIGEST_SCHEMA
    assert channels["echo"].metadata == {}


def test_off_writes_no_message_and_keeps_the_channel_declared(recordings):
    _, _, full = mcap(recordings["full"])
    channels, schemas, off = mcap(recordings["off"])

    assert off == [m for m in full if m[0] != "ticks"]
    ticks = channels["ticks"]
    assert ticks.metadata == {"sil_record": "off"}
    assert schemas[ticks.schema_id].name == "toy.Counter"


def test_two_runs_of_a_digest_channel_give_identical_recordings(
        sil_run, tmp_path, recordings):
    again = run(sil_run, producer_manifest("digest"), tmp_path / "again.mcap")

    assert again.read_bytes() == recordings["digest"].read_bytes()


@pytest.mark.parametrize("value", ["none", 1, None])
def test_the_kernel_refuses_an_unknown_record_mode(sil_run, tmp_path, value):
    doc = producer_manifest(None).to_doc()
    doc["channels"]["ticks"]["record"] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))

    proc = run_manifest(sil_run, path, tmp_path / "bad.mcap")

    assert proc.returncode == 2
    assert "channel 'ticks'" in proc.stderr and "record" in proc.stderr
