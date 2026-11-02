"""SHA-256 backend agreement and its build dependency (#325).

Python's hashlib is the independent implementation. tests/test_provenance.py
covers file digests at the Run boundary, and tests/test_replayer.py covers a
Replay hash mismatch.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Each side of a padding or block boundary: 55/56 is where the length no longer
# fits in the block, and 119/120 is the same point one block later.
LENGTHS = [0, 1, 3, 55, 56, 57, 63, 64, 65, 119, 120, 121, 127, 128, 129,
           1000, 65535, 65536, 65537]
LARGE_LENGTH = 16 * 1024 * 1024 + 13
CHUNKINGS = ["1", "3,61", "63,1,65", "64", "65", "4096", "65536"]


def binary_bytes(length: int) -> bytes:
    return random.Random(length).randbytes(length)


@pytest.fixture(scope="module")
def digest_tool(build_dir: Path) -> Path:
    return build_dir / "sil_sha256_digest"


def kernel_digest(tool: Path, path: Path, chunks: str) -> str:
    return subprocess.run([str(tool), str(path), chunks], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.mark.parametrize("length", LENGTHS)
@pytest.mark.parametrize("chunks", CHUNKINGS)
def test_digest_agrees_with_an_independent_implementation(
        digest_tool: Path, tmp_path: Path, length: int, chunks: str):
    if chunks == "1" and length > 1000:
        pytest.skip("single-byte updates of a large input add only run time")
    data = binary_bytes(length)
    path = tmp_path / "input.bin"
    path.write_bytes(data)
    assert kernel_digest(digest_tool, path, chunks) == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("chunks", ["65536", "4096,7", "1048576"])
def test_large_input_agrees_with_an_independent_implementation(
        digest_tool: Path, tmp_path: Path, chunks: str):
    data = binary_bytes(LARGE_LENGTH)
    path = tmp_path / "large.bin"
    path.write_bytes(data)
    assert kernel_digest(digest_tool, path, chunks) == hashlib.sha256(data).hexdigest()


def test_reference_and_kernel_backends_produce_one_digest(
        digest_tool: Path, tmp_path: Path):
    """The measurement compares two backends, so both must compute one value."""
    data = binary_bytes(1 << 20)
    path = tmp_path / "input.bin"
    path.write_bytes(data)
    lines = {
        subprocess.run([str(digest_tool), str(path), "65536", "--backend", backend],
                       capture_output=True, text=True, check=True).stdout.split()[0]
        for backend in ("kernel", "minimal")
    }
    assert lines == {hashlib.sha256(data).hexdigest()}


def kernel_build_sources() -> list[Path]:
    """Each script, image and workflow that configures the kernel from source."""
    roots = [ROOT / "Dockerfile", ROOT / "models", ROOT / "proofs",
             ROOT / ".github" / "workflows"]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend([root] if root.is_file() else root.rglob("*"))
    return sorted(
        path for path in candidates
        if path.is_file()
        and (path.suffix in {".sh", ".yml"} or "Dockerfile" in path.name)
        and re.search(r"cmake\s+-S\b", path.read_text(errors="replace"))
    )


def test_each_kernel_build_path_installs_the_openssl_build_dependency():
    """The runner links libcrypto statically: only build stages need the package."""
    sources = kernel_build_sources()
    assert ROOT / "Dockerfile" in sources
    assert ROOT / "models/can/Dockerfile" in sources
    missing = []
    for path in sources:
        # A proof script builds on a host that its workflow prepares.
        prepared = [path]
        if path.name == "run-proof.sh":
            prepared.append(ROOT / ".github" / "workflows" /
                            f"proof-{path.parent.name}.yml")
        if not any("libssl-dev" in candidate.read_text() for candidate in prepared):
            missing.append(str(path.relative_to(ROOT)))
    assert not missing, f"no libssl-dev for: {missing}"


def test_openssl_build_dependency_stays_out_of_the_runtime_stage():
    runtime = (ROOT / "Dockerfile").read_text().split(" AS runtime", 1)[1]
    assert "libssl-dev" not in runtime


def test_runner_loads_no_shared_openssl(sil_run: Path):
    """A shared libcrypto would enter the process of a Native Participant."""
    inspect = ["otool", "-L"] if sys.platform == "darwin" else ["ldd"]
    libraries = subprocess.run([*inspect, str(sil_run)], capture_output=True,
                               text=True, check=True).stdout
    assert "libcrypto" not in libraries
    assert "libssl" not in libraries


def test_host_openssl_configuration_does_not_reach_a_digest(
        digest_tool: Path, tmp_path: Path):
    """A malformed configuration would otherwise be read on first use."""
    config = tmp_path / "openssl.cnf"
    config.write_text("[[[ not a configuration\nopenssl_conf = missing\n")
    data = binary_bytes(100)
    path = tmp_path / "input.bin"
    path.write_bytes(data)
    report = subprocess.run([str(digest_tool), str(path), "64"], capture_output=True,
                            text=True, check=True,
                            env={**os.environ, "OPENSSL_CONF": str(config)})
    assert report.stdout.strip() == hashlib.sha256(data).hexdigest()
    assert report.stderr == ""
