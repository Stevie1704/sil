"""Run-boundary behaviour the large-Message routing baseline rests on (#61).

The baseline has to be able to run a Manifest without a Recording, so the cost
of routing to subscribers can be told apart from the cost of Recording I/O.
Nothing here asserts a duration.
"""

import json
import subprocess

from conftest import BUILD_DIR, ROOT

from sil.manifest import Manifest

BENCH_SCHEMAS = json.loads((ROOT / "schemas" / "bench.json").read_text())

PERIOD_NS = 10_000_000
DURATION_NS = 100_000_000
PUBLISHES = DURATION_NS // PERIOD_NS
SMALL_BYTES = 16


def native_manifest(tmp_path, *, subscribers: int):
    """One native source publishing bench.Small to `subscribers` native sinks."""
    m = Manifest(duration_ns=DURATION_NS)
    m.add_schemas({"bench.Small": BENCH_SCHEMAS["bench.Small"]})
    m.add_channel("frames", schema="bench.Small")
    m.add_native(
        "source",
        library=str(BUILD_DIR / "bench_source.silp"),
        config={"channel": "frames", "bytes": SMALL_BYTES,
                "period_ns": PERIOD_NS, "burst": 1},
        publishes=["frames"],
    )
    for i in range(subscribers):
        m.add_native(
            f"sink{i}",
            library=str(BUILD_DIR / "bench_sink.silp"),
            config={"input": "frames", "period_ns": PERIOD_NS},
            subscribes=["frames"],
        )
    return m.write(tmp_path / f"bench{subscribers}.json")


class TestRecordingSwitch:
    """Recording-off has to be reachable from the run boundary."""

    def test_run_without_recording_writes_no_file(self, sil_run, tmp_path):
        ref = native_manifest(tmp_path, subscribers=1)
        proc = subprocess.run(
            [str(sil_run), str(ref.path), "--no-recording"],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("manifest_hash ")
        assert list(tmp_path.glob("*.mcap")) == []

    def test_output_path_with_no_recording_is_a_config_error(
        self, sil_run, tmp_path
    ):
        ref = native_manifest(tmp_path, subscribers=1)
        proc = subprocess.run(
            [str(sil_run), str(ref.path), "--no-recording",
             "-o", str(tmp_path / "out.mcap")],
            capture_output=True, text=True,
        )
        assert proc.returncode == 2
        assert not (tmp_path / "out.mcap").exists()
