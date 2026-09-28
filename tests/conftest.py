import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = Path(os.environ.get("SIL_BUILD_DIR", ROOT / "build"))

# Existing behavior suites are not capacity tests. This finite ceiling is well
# above their declared bursts so migrating them to issue #75 does not turn an
# old behavior assertion into an accidental overflow-policy assertion.
COMPAT_ROUTE_CAPACITY = 1024

# Make the sil package importable here and in every child process the kernel
# spawns (Python step participants), independent of install state.
# (Editable installs are unreliable here: Python 3.14 skips .pth files that
# macOS flags UF_HIDDEN, which uv-created venvs do.)
_SRC = ROOT / "python" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
os.environ["PYTHONPATH"] = str(_SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")


def load_module(name: str, path: Path):
    """Load a Python file that is not in a package, such as an example's."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.fixture(scope="session")
def sil_run_instrumented(build_dir) -> Path:
    """The same kernel built with payload-copy counters (issue #61)."""
    exe = build_dir / "sil-run-instrumented"
    assert exe.exists(), f"instrumented runner not built at {exe}"
    return exe


def run_manifest(runner: Path, manifest_path: Path, out: Path,
                 env: dict[str, str] | None = None):
    """One Run at the run boundary: manifest in, exit code + MCAP out.

    The runner is an argument because the production and the instrumented
    kernel have to be run the same way to be compared (issue #82).
    """
    # `env=None` inherits this process's environment, so only callers that
    # need to scope the run (e.g. TMPDIR) pass one.
    proc = subprocess.run(
        [str(runner), str(manifest_path), "-o", str(out)],
        capture_output=True, text=True, env=env,
    )
    proc.mcap_path = out
    return proc


@pytest.fixture
def run_sil(sil_run, tmp_path):
    """Invoke the production runner, with an output path if none is given."""

    def _run(manifest_path: Path, out: Path | None = None,
             env: dict[str, str] | None = None):
        return run_manifest(sil_run, manifest_path, out or tmp_path / "out.mcap",
                            env)

    return _run


# A staged installation: the wheel in its own venv and the CMake install
# prefix, with no source tree on any import path.
def uv_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(ROOT / "build" / "uv-cache"))
    return env


@pytest.fixture(scope="session")
def wheel_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheel_dir = tmp_path_factory.mktemp("sil-wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir),
         str(ROOT / "python")],
        check=True, env=uv_environment(),
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_dir.glob("sil-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture(scope="session")
def installed_python(
    wheel_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    venv = tmp_path_factory.mktemp("sil-venv")
    env = uv_environment()
    subprocess.run(
        ["uv", "venv", "-p", sys.executable, str(venv)],
        check=True, env=env,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["uv", "pip", "install", "-p", str(venv / "bin" / "python"),
         str(wheel_path)],
        check=True, env=env,
        capture_output=True,
        text=True,
    )
    return venv


@pytest.fixture(scope="session")
def staged_prefix(build_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    prefix = tmp_path_factory.mktemp("sil-prefix")
    subprocess.run(
        ["cmake", "--install", str(build_dir), "--prefix", str(prefix)],
        check=True,
        capture_output=True,
        text=True,
    )
    return prefix
