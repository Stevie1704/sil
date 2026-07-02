import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = Path(os.environ.get("SIL_BUILD_DIR", ROOT / "build"))

# Make the sil package importable here and in every child process the kernel
# spawns (Python step participants), independent of install state.
# (Editable installs are unreliable here: Python 3.14 skips .pth files that
# macOS flags UF_HIDDEN, which uv-created venvs do.)
_SRC = ROOT / "python" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
os.environ["PYTHONPATH"] = str(_SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")


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
