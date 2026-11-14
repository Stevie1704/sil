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


def replay_manifest(recording, channels=("ticks",)):
    m = toy_manifest(duration_ns=DURATION_NS)
    for channel in channels:
        m.add_channel(channel, schema="toy.Counter")
    m.add_replay("replay", recording=recording, channels=list(channels))
    return m


@pytest.mark.parametrize("mode", ["digest", "off"])
def test_replay_refuses_a_channel_recorded_without_payload(
        sil_run, tmp_path, recordings, mode):
    ref = replay_manifest(recordings[mode]).write(tmp_path / "replay.json")

    proc = run_manifest(sil_run, ref.path, tmp_path / "replay.mcap")

    assert proc.returncode == 2
    assert (f"channel 'ticks' is recorded with record mode '{mode}'"
            in proc.stderr)


def test_replay_takes_a_full_channel_from_a_recording_with_a_digest_channel(
        sil_run, tmp_path, recordings):
    ref = replay_manifest(recordings["digest"], channels=("echo",)).write(
        tmp_path / "replay.json")

    proc = run_manifest(sil_run, ref.path, tmp_path / "replay.mcap")

    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("mode", ["digest", "off"])
def test_validate_refuses_a_replay_of_a_channel_without_payload(
        tmp_path, recordings, mode):
    from sil.manifest_validate import validate

    ref = replay_manifest(recordings[mode]).write(tmp_path / "replay.json")

    problems = validate(ref.path)

    assert [(p.path, p.message) for p in problems] == [
        ("/participants/replay/channels/0",
         f"channel 'ticks' is recorded with record mode '{mode}': the "
         "Recording has no payload to replay")]


@pytest.mark.parametrize("mode", ["digest", "off"])
def test_a_replay_window_refuses_a_channel_without_payload(
        tmp_path, recordings, mode):
    from sil.replay_window import WindowError, prepare

    doc = tmp_path / "window.json"
    doc.write_text(json.dumps({
        "sil_replay_window": 1, "source_origin_ns": 0, "replay_start_ns": 0,
        "evaluation_start_ns": TICK_NS, "end_ns": DURATION_NS,
        "channels": ["ticks"]}))

    with pytest.raises(WindowError, match=(
            f"channel 'ticks' is recorded with record mode '{mode}'")):
        prepare(doc, recordings[mode], tmp_path / "window.mcap")
    assert not (tmp_path / "window.mcap").exists()


def check(sil_run, manifest_path, *argv):
    import subprocess

    return subprocess.run(
        [sys.executable, "-m", "sil.check", "--runner", str(sil_run),
         str(manifest_path), *argv],
        capture_output=True, text=True)


def test_check_names_the_channels_recorded_without_payload(sil_run, tmp_path):
    m = producer_manifest("digest")
    m.add_channel("debug", schema="toy.Counter", record="off")
    ref = m.write(tmp_path / "check.json")

    proc = check(sil_run, ref.path, "--json")

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["verdict"] == "deterministic"
    assert report["record_modes"] == {"digest": ["ticks"], "off": ["debug"]}
    text = check(sil_run, ref.path)
    assert text.returncode == 0, text.stderr
    assert text.stdout.splitlines()[1:] == [
        "  digest only (payload not compared, only its SHA-256): ticks",
        "  not recorded (not compared): debug"]


def test_check_report_of_a_full_recording_names_no_channel(sil_run, tmp_path):
    ref = producer_manifest(None).write(tmp_path / "check.json")

    proc = check(sil_run, ref.path, "--json")

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["record_modes"] == {"digest": [], "off": []}
    assert len(check(sil_run, ref.path).stdout.splitlines()) == 1


def test_check_finds_a_changed_payload_of_a_digest_channel(sil_run, tmp_path):
    m = toy_manifest(duration_ns=DURATION_NS)
    m.add_channel("echo", schema="toy.Counter", record="digest")
    m.add_process(
        "noisy",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "nondet.py")],
        step_period_ns=TICK_NS,
        publishes=["echo"],
    )
    ref = m.write(tmp_path / "nondet.json")

    proc = check(sil_run, ref.path, "--json")

    assert proc.returncode == 3
    report = json.loads(proc.stdout)
    assert report["verdict"] == "violation"
    assert report["first_difference"]["kind"] == "message"
    assert report["first_difference"]["channel"] == "echo"
    assert report["record_modes"] == {"digest": ["echo"], "off": []}


def sil(capsys, *argv):
    from sil import cli

    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_recording_channels_states_the_record_mode_of_each_channel(
        capsys, recordings):
    code, out, _ = sil(capsys, "recording", "channels",
                       str(recordings["digest"]), "--json")

    assert code == 0
    rows = {row["channel"]: row for row in json.loads(out)["channels"]}
    assert rows["ticks"] == {
        "channel": "ticks", "schema": "sil.Sha256Digest", "record": "digest",
        "source_schema": "toy.Counter", "messages": 5, "first_t_ns": 0,
        "last_t_ns": 4 * TICK_NS}
    assert rows["echo"]["record"] == "full"
    assert rows["echo"]["source_schema"] == "toy.Counter"

    code, out, _ = sil(capsys, "recording", "channels",
                       str(recordings["off"]), "--json")
    rows = {row["channel"]: row for row in json.loads(out)["channels"]}
    assert rows["ticks"] == {
        "channel": "ticks", "schema": "toy.Counter", "record": "off",
        "source_schema": "toy.Counter", "messages": 0, "first_t_ns": None,
        "last_t_ns": None}


def test_recording_show_prints_the_digest_of_a_digest_channel(
        capsys, recordings):
    _, _, full = mcap(recordings["full"])
    expected = [{"channel": "ticks", "t_ns": t,
                 "fields": {"sha256": hashlib.sha256(data).hexdigest()}}
                for topic, t, data in full if topic == "ticks"]

    code, out, _ = sil(capsys, "recording", "show", str(recordings["digest"]),
                       "--channel", "ticks", "--json")

    assert code == 0
    assert [json.loads(line) for line in out.splitlines()] == expected


def test_recording_query_names_the_digest_when_a_payload_field_is_asked(
        capsys, recordings):
    code, out, err = sil(capsys, "recording", "query",
                         str(recordings["digest"]), "--channel", "ticks",
                         "--field", "value")

    assert code == 2
    assert json.loads(out)["error"] == (
        "digest Channel, only 'sha256': unknown or invalid field 'value'")
    assert "unknown or invalid field 'value'" in err


def test_run_simulation_gives_the_digest_fields_of_a_digest_channel(
        sil_run, tmp_path, recordings):
    from sil.testing import run_simulation

    _, _, full = mcap(recordings["full"])
    result = run_simulation(producer_manifest("digest"), runner=sil_run,
                            workdir=tmp_path)

    assert result.messages("ticks") == [
        (t, {"sha256": hashlib.sha256(data).digest()})
        for topic, t, data in full if topic == "ticks"]
    assert result.messages("echo") == [
        (t, {"seq": t // TICK_NS, "value": -(t // TICK_NS)})
        for topic, t, _ in full if topic == "echo"]


def contract(tmp_path, *fields: str):
    path = tmp_path / f"contract-{'-'.join(fields)}.json"
    path.write_text(json.dumps({
        "sil_comparison": 1,
        "evaluation": {"from_ns": 0, "to_ns": DURATION_NS},
        "channels": {"ticks": {
            "actual_offset_ns": 0, "reference_offset_ns": 0,
            "observations": {"start_ns": 0, "stop_ns": 4 * TICK_NS,
                             "step_ns": TICK_NS},
            "fields": {field: "exact" for field in fields}}}}))
    return path


def test_compare_checks_the_digests_of_two_digest_recordings(
        capsys, tmp_path, recordings, sil_run):
    again = run(sil_run, producer_manifest("digest"), tmp_path / "again.mcap")

    code, out, err = sil(capsys, "compare", str(contract(tmp_path, "sha256")),
                         str(recordings["digest"]), str(again), "--json")

    assert code == 0, err
    assert json.loads(out)["verdict"] == "pass"


def test_compare_fails_on_a_payload_field_of_a_digest_channel(
        capsys, tmp_path, recordings):
    code, out, _ = sil(capsys, "compare", str(contract(tmp_path, "value")),
                       str(recordings["digest"]), str(recordings["digest"]),
                       "--json")

    assert code == 1
    report = json.loads(out)
    assert report["verdict"] == "fail"
    assert "names field 'value', which its schema does not declare" in out


def test_query_of_an_off_channel_is_complete_with_no_result(
        capsys, recordings):
    code, out, _ = sil(capsys, "recording", "query", str(recordings["off"]),
                       "--channel", "ticks", "--field", "value")

    assert code == 0
    report = json.loads(out)
    assert report["status"] == "complete" and report["results"] == []


@pytest.mark.parametrize("mode", ["digest", "off"])
def test_export_writes_a_digest_or_off_channel(
        capsys, tmp_path, recordings, mode):
    out_path = tmp_path / "export.mcap"
    code, _, err = sil(capsys, "recording", "export", str(recordings[mode]),
                       "--profile", "visualization-json", "-o",
                       str(out_path))

    assert code == 0, err
    channels, _, messages = mcap(out_path)
    assert "ticks" in channels
    assert len([m for m in messages if m[0] == "ticks"]) == (
        5 if mode == "digest" else 0)


def test_compare_fails_on_an_off_channel(capsys, tmp_path, recordings):
    code, out, _ = sil(capsys, "compare",
                       str(contract(tmp_path, "seq", "value")),
                       str(recordings["off"]), str(recordings["full"]),
                       "--json")

    assert code == 1
    report = json.loads(out)
    assert report["verdict"] == "fail"
    assert report["coverage"] == []
    assert report["channels"]["ticks"]["missing_actual"] == 5



@pytest.mark.parametrize("mode", ["full", "digest", "off"])
def test_validate_accepts_each_record_mode(capsys, tmp_path, mode):
    doc = producer_manifest(None).to_doc()
    doc["channels"]["ticks"]["record"] = mode
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc))

    code, out, _ = sil(capsys, "manifest", "validate", str(path), "--json")

    assert code == 0, out
    assert json.loads(out)["errors"] == []


@pytest.mark.parametrize("value", ["none", 1, None])
def test_validate_refuses_an_unknown_record_mode(capsys, tmp_path, value):
    doc = producer_manifest(None).to_doc()
    doc["channels"]["ticks"]["record"] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc))

    code, out, _ = sil(capsys, "manifest", "validate", str(path), "--json")

    assert code == 2
    assert [p["path"] for p in json.loads(out)["errors"]] == [
        "/channels/ticks/record"]
