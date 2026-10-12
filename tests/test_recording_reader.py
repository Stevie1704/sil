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
    from toys import add_producer, toy_manifest

    m = toy_manifest(duration_ns=30_000_000)
    m.add_channel("ticks", schema="toy.Counter")
    add_producer(m, "producer", channel="ticks", period_ns=10_000_000)
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


def test_stored_order_reader_does_not_buffer_all_messages_before_first_yield(tmp_path):
    """Read-side resource contract: one small chunk, not all selected payloads."""
    import tracemalloc

    from mcap.writer import CompressionType, Writer

    path = tmp_path / "long.mcap"
    with path.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.NONE, chunk_size=65536)
        writer.start()
        channel = writer.register_channel("signal", "raw", 0)
        for i in range(30000):
            writer.add_message(channel, log_time=i, publish_time=i, data=b"x" * 128)
        writer.finish()
    records = read_records(path, channels=["signal"])
    tracemalloc.start()
    try:
        assert next(records) == ("signal", 0, b"x" * 128)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        records.close()
    assert peak < 4 * 1024 * 1024, f"first Message allocated {peak} bytes"


@pytest.mark.parametrize("chunking,indexed", [(True, True), (True, False), (False, False)])
def test_reader_preserves_stored_order_and_filters_across_chunk_boundaries(
        tmp_path, chunking, indexed):
    from mcap.writer import CompressionType, IndexType, Writer

    path = tmp_path / "order.mcap"
    expected = [("left", 20, b"a"), ("right", 10, b"b"),
                ("left", 10, b"c"), ("left", 10, b"d"), ("left", 30, b"e")]
    with path.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.NONE, chunk_size=1,
                        use_chunking=chunking,
                        index_types=IndexType.ALL if indexed else IndexType.NONE)
        writer.start()
        channels = {name: writer.register_channel(name, "raw", 0)
                    for name in ("left", "right")}
        for name, t, data in expected:
            writer.add_message(channels[name], log_time=t, publish_time=t, data=data)
        writer.finish()
    assert list(read_records(path)) == expected
    assert list(read_records(path, channels=["left"], start_ns=10, end_ns=20)) == [
        ("left", 10, b"c"), ("left", 10, b"d")]
    assert list(read_records(path, channels=frozenset({"left"}), start_ns=10, end_ns=20)) == [
        ("left", 10, b"c"), ("left", 10, b"d")]
    assert list(read_records(path, channels=[])) == []
