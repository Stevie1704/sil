"""Offline acceptance and resource evidence for issue #282.

Uses the retained ADAS failure, plus a synthetic long stream of its command
payloads with one known excursion. No new Run or domain trajectory is claimed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import struct
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from mcap.reader import NonSeekingReader, make_reader
from mcap.writer import CompressionType, Writer

from sil.recording_query import Predicate, RecordingQuery

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / ("proofs/agent-investigation/evidence/trial-1/ci/cases/"
               "delay-regression/runs/delay-regression/run-1.mcap")
SEED_SHA256 = "02e4b4225de5dd20e51ddd06e9651b885e07182da36cf5d00c1fb236d708fff2"
CHANNEL = "cadence.command"
FIELD = "radar_age_ns"
THRESHOLD = 40000000


def seed_evidence() -> tuple[dict, bytes, bytes, bytes]:
    """Independent binary scan of the published ADAS C layouts."""
    assert hashlib.sha256(SEED.read_bytes()).hexdigest() == SEED_SHA256
    with SEED.open("rb") as stream:
        reader = make_reader(stream)
        messages = [(schema, message) for schema, _, message in reader.iter_messages(
            topics=[CHANNEL], log_time_order=False)]
    ages = [(m.log_time, struct.unpack_from("<q", m.data, 28)[0]) for _, m in messages]
    assert ages[8:11] == [(80000000, 40000000), (90000000, 50000000),
                          (100000000, 60000000)]
    source = RecordingQuery(SEED)
    event = source.query(CHANNEL, [FIELD, "mode"],
                         where=Predicate(FIELD, "cross-up", THRESHOLD))
    assert [(r["t_ns"], r["message_index"], r["fields"][FIELD])
            for r in event["results"]] == [(90000000, 9, 50000000)]
    summary = source.query(CHANNEL, [FIELD], mode="summary")
    assert summary["results"][0]["max"]["value"] == max(age for _, age in ages)
    nearby = source.query("cadence.radar", ["sample_time_ns", "object_id[0]", "x_m[0]"],
                          start_ns=80000000, end_ns=120000000)
    with SEED.open("rb") as stream:
        reference = [(m.log_time, struct.unpack_from("<Q", m.data)[0],
                      struct.unpack_from("<i", m.data, 25)[0],
                      struct.unpack_from("<f", m.data, 57)[0])
                     for _, _, m in make_reader(stream).iter_messages(
                         topics=["cadence.radar"], start_time=80000000,
                         end_time=120000000, log_time_order=False)]
    assert reference == [(110000000, 60000000, 30, 50.0),
                         (100000000, 100000000, 30, 50.0)]
    assert [(r["t_ns"], r["fields"]["sample_time_ns"], r["fields"]["object_id[0]"],
             r["fields"]["x_m[0]"]) for r in nearby["results"]] == reference
    schema = messages[0][0]
    assert schema is not None
    return ({"event": event, "summary": summary, "nearby_inputs": nearby},
            schema.data, messages[0][1].data, messages[9][1].data)


def expand(path: Path, count: int, chunk_size: int, schema: bytes,
           normal: bytes, excursion: bytes) -> int:
    event_index = count // 2 + 1
    assert event_index % 100 != 0
    with path.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.NONE, chunk_size=chunk_size)
        writer.start(profile="sil", library="sil-recording-query-proof")
        schema_id = writer.register_schema("adas.Command", "sil_pod", schema)
        channel = writer.register_channel(CHANNEL, "sil_pod", schema_id)
        for index in range(count):
            t = index * 10000000
            writer.add_message(channel, log_time=t, publish_time=t,
                               data=excursion if index == event_index else normal)
        writer.finish()
    return event_index


def direct_scan(path: Path, event_index: int) -> dict:
    matches = []
    downsample_matches = 0
    with path.open("rb") as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        assert summary is not None
        chunks = len(summary.chunk_indexes)
        stream.seek(0)
        for index, (_, _, message) in enumerate(NonSeekingReader(stream).iter_messages(
                topics=[CHANNEL], log_time_order=False)):
            value = struct.unpack_from("<q", message.data, 28)[0]
            if value > THRESHOLD:
                matches.append((index, message.log_time, value))
                downsample_matches += index % 100 == 0
    assert matches == [(event_index, event_index * 10000000, 50000000)]
    assert downsample_matches == 0
    return {"matches": matches, "every_100th_message_matches": downsample_matches,
            "chunks": chunks}


def measure(launcher: Path, path: Path, count: int, out: Path) -> dict:
    metrics = out / (path.stem + ".cost.txt")
    command = [str(launcher), str(metrics), sys.executable, "-m", "sil.cli",
               "recording", "query", str(path), "--channel", CHANNEL,
               "--field", FIELD, "--where", FIELD, "cross-up", str(THRESHOLD),
               "--max-results", "1", "--max-bytes", "4096"]
    completed = subprocess.run(command, capture_output=True, check=True)
    code, wall_ns, user_us, system_us, rss = map(int, metrics.read_text().split())
    assert code == 0 and completed.stderr == b""
    report = json.loads(completed.stdout)
    assert report["status"] == "complete" and report["scan"]["messages"] == count
    assert report["scan"]["matches"] == 1
    assert len(completed.stdout) == report["output"]["serialized_bytes"] <= 4096
    peak_rss = rss if sys.platform == "darwin" else rss * 1024
    return {"report": report, "peak_rss_bytes": peak_rss, "wall_ns": wall_ns,
            "user_us": user_us, "system_us": system_us,
            "output_bytes": len(completed.stdout), "recording_bytes": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--counts", type=int, nargs="+", default=[10000, 1000000])
    args = parser.parse_args()
    if any(n < 1000 or n % 100 for n in args.counts):
        parser.error("counts must be multiples of 100 and at least 1000")
    args.out.mkdir(parents=True, exist_ok=True)
    evidence, schema, normal, excursion = seed_evidence()
    launcher = args.out / "run_measured"
    subprocess.run(["cc", "-O2", str(ROOT / "proofs/adas-cost/run_measured.c"),
                    "-o", str(launcher)], check=True)
    results = []
    for chunk_size in (65536, 1048576):
        for count in args.counts:
            path = args.out / f"commands-{count}-{chunk_size}.mcap"
            event_index = expand(path, count, chunk_size, schema, normal, excursion)
            direct = direct_scan(path, event_index)
            measured = measure(launcher, path, count, args.out)
            row = measured["report"]["results"][0]
            assert (row["message_index"], row["t_ns"], row["fields"][FIELD]) == tuple(
                direct["matches"][0])
            results.append({"messages": count, "chunk_size": chunk_size,
                            "reference": direct, **measured})
    result = {"sil_recording_query_evidence": 1,
              "machine": {"os": platform.system(), "architecture": platform.machine(),
                          "python": platform.python_version(), "mcap": version("mcap")},
              "implementation": {
                  name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                  for name in ("python/src/sil/recording.py", "python/src/sil/recording_query.py",
                               "proofs/recording-query/verify.py", "proofs/adas-cost/run_measured.c")},
              "seed": {"path": str(SEED.relative_to(ROOT)), "sha256": SEED_SHA256,
                       "queries": evidence},
              "measurements": results}
    (args.out / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Verified ADAS evidence and {len(results)} long scans: {args.out / 'evidence.json'}")


if __name__ == "__main__":
    main()
