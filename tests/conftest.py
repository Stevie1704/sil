import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = Path(os.environ.get("SIL_BUILD_DIR", ROOT / "build"))


@pytest.fixture(scope="session")
def build_dir() -> Path:
    """Configure and build the kernel once per test session."""
    if not os.environ.get("SIL_SKIP_BUILD"):
        if not (BUILD_DIR / "CMakeCache.txt").exists():
            subprocess.run(
                ["cmake", "-S", str(ROOT), "-B", str(BUILD_DIR),
                 "-DCMAKE_BUILD_TYPE=Release"],
                check=True,
            )
        subprocess.run(
            ["cmake", "--build", str(BUILD_DIR), "-j"],
            check=True, capture_output=True, text=True,
        )
    return BUILD_DIR


@pytest.fixture(scope="session")
def sil_run(build_dir) -> Path:
    exe = build_dir / "sil-run"
    assert exe.exists(), f"kernel runner not built at {exe}"
    return exe


@pytest.fixture
def run_sil(sil_run, tmp_path):
    """Invoke the runner at the run boundary: manifest in, exit code + MCAP out."""

    def _run(manifest_path: Path, out: Path | None = None):
        out = out or tmp_path / "out.mcap"
        proc = subprocess.run(
            [str(sil_run), str(manifest_path), "-o", str(out)],
            capture_output=True, text=True,
        )
        proc.mcap_path = out
        return proc

    return _run
