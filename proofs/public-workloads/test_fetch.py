"""A fetched source is accepted only if it is the exact pinned artifact."""
import hashlib
import json
from pathlib import Path

import pytest
from fetch import SOURCES, verify

DATA = b"recorded bytes"
PIN = {"sha256": hashlib.sha256(DATA).hexdigest(), "size": len(DATA)}


def test_the_pinned_bytes_pass():
    verify("sample", DATA, PIN)


@pytest.mark.parametrize("data", [DATA + b"!", DATA[:-1], b"recorded bytez"])
def test_changed_bytes_fail_naming_the_source(data):
    with pytest.raises(RuntimeError, match="sample"):
        verify("sample", data, PIN)


def test_every_file_source_pins_digest_size_and_license():
    sources = json.loads(Path(SOURCES).read_text())
    for name, source in sources.items():
        assert source["license"], name
        if source["kind"] == "file":
            assert len(source["sha256"]) == 64 and source["size"] > 0, name
        else:
            assert len(source["commit"]) == 40 and len(source["tree"]) == 40, name
