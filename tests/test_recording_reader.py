"""Python read side of the recording-format seam: the reader entry point
dispatches on the output extension and rejects an unrecognized one with a
clear error, mirroring the kernel's config-time rejection (req #24)."""

import pytest

from sil.recording import UnknownRecordingFormat, read_records


def test_unknown_extension_is_rejected_with_a_clear_error(tmp_path):
    path = tmp_path / "out.bogus"
    path.write_bytes(b"not a recording")
    with pytest.raises(UnknownRecordingFormat, match=r"\.bogus"):
        # read_records is a generator; force it to run.
        list(read_records(path))


def test_mcap_extension_reads_recorded_messages(sil_run, tmp_path):
    # A real .mcap produced by the kernel round-trips through the reader.
    from toys import producer_library, toy_manifest

    m = toy_manifest(duration_ns=30_000_000)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_native(
        "producer",
        library=producer_library(),
        config={"channel": "ticks", "period_ns": 10_000_000},
    )
    out = tmp_path / "out.mcap"
    import subprocess

    proc = subprocess.run(
        [str(sil_run), str(m.write(tmp_path / "m.json").path), "-o", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    records = list(read_records(out))
    assert [t for _, t, _ in records] == [i * 10_000_000 for i in range(3)]
    assert all(topic == "ticks" for topic, _, _ in records)
