"""`tools/install.sh`: install a released SiL version from its GitHub release.

The tests serve a fake release over file:// through SIL_RELEASE_URL: a wheel
without dependencies and shell scripts in place of the native runners, so pip
needs no index. A fake `uname` first on PATH makes any host look like Linux
x86-64. The release workflow installs the real artifacts with the same script.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))

from release import ReleaseError, project_version, stamp_installer  # noqa: E402

SCRIPT = ROOT / "tools" / "install.sh"
NATIVE = ("sil-run", "sil-run-diagnostics", "sil-diagnostics-launcher", "silschema")


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode()


def _wheel(version: str) -> bytes:
    """A minimal `sil` wheel whose `sil` command prints its version."""
    info = f"sil-{version}.dist-info"
    files = {
        "sil/__init__.py": f"def main():\n    print('sil {version}')\n".encode(),
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: sil\nVersion: {version}\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                         b"Tag: py3-none-any\n",
        f"{info}/entry_points.txt": b"[console_scripts]\nsil = sil:main\n",
    }
    record = "".join(f"{name},{_record_hash(data)},{len(data)}\n"
                     for name, data in files.items()) + f"{info}/RECORD,,\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr(f"{info}/RECORD", record)
    return buffer.getvalue()


def _native_archive(version: str) -> bytes:
    """A native archive whose runners report `version`."""
    top = f"sil-native-dev-{version}-linux-x86_64"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in NATIVE:
            data = f"#!/bin/sh\necho {version}\n".encode()
            member = tarfile.TarInfo(f"{top}/bin/{name}")
            member.size, member.mode = len(data), 0o755
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def publish(mirror: Path, version: str) -> Path:
    """Write the downloadable files of release `version` under `mirror`."""
    release = mirror / f"v{version}"
    release.mkdir(parents=True)
    (release / f"sil-{version}-py3-none-any.whl").write_bytes(_wheel(version))
    (release / f"sil-native-dev-{version}-linux-x86_64.tar.gz").write_bytes(
        _native_archive(version))
    (release / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(release.iterdir())))
    return release


def _can_create_a_venv_with_pip() -> bool:
    proc = subprocess.run([sys.executable, "-c", "import ensurepip"],
                          capture_output=True)
    return proc.returncode == 0


needs_venv = pytest.mark.skipif(
    not _can_create_a_venv_with_pip(),
    reason="the test Python has no ensurepip (Debian and Ubuntu: python3-venv)")


@pytest.fixture
def host(tmp_path: Path) -> dict[str, str]:
    """The environment of a Linux x86-64 host without network access for pip."""
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    uname = fake / "uname"
    uname.write_text('#!/bin/sh\ncase "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac\n')
    uname.chmod(0o755)
    python = Path(sys.executable).parent
    return {
        "PATH": os.pathsep.join([str(fake), str(python), os.environ["PATH"]]),
        "HOME": str(tmp_path / "home"),
        "SIL_RELEASE_URL": (tmp_path / "mirror").as_uri(),
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def install(env: dict[str, str], *argv: str, script: Path = SCRIPT
            ) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(script), *argv], env=env,
                          capture_output=True, text=True, timeout=300)


def in_env(prefix: Path, command: str, env: dict[str, str]) -> str:
    """The stdout of `command` in a shell that sourced PREFIX/env."""
    proc = subprocess.run(["sh", "-c", f'. "{prefix}/env" && {command}'],
                          env=env, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@needs_venv
def test_install_puts_the_release_on_path(tmp_path, host):
    publish(tmp_path / "mirror", "0.2.0")
    prefix = tmp_path / "sil"

    proc = install(host, "--version", "0.2.0", "--prefix", str(prefix))

    assert proc.returncode == 0, proc.stderr
    assert f'. "{prefix}/env"' in proc.stdout
    assert os.readlink(prefix / "current") == "0.2.0"
    assert in_env(prefix, "sil-run --version", host) == "0.2.0"
    assert in_env(prefix, "sil", host) == "sil 0.2.0"
    for name in NATIVE:
        assert in_env(prefix, f"command -v {name}", host) == \
            str(prefix / "current" / "native" / "bin" / name)
    # Manifests start `python3 -m sil.participant`: python3 must be the venv's.
    package = Path(in_env(prefix, "python3 -c 'import sil; print(sil.__file__)'", host))
    assert package.resolve().is_relative_to((prefix / "0.2.0" / "venv").resolve())
    assert sorted(path.name for path in prefix.iterdir()) == ["0.2.0", "current", "env"]


def test_a_download_that_does_not_match_its_checksum_installs_nothing(tmp_path, host):
    release = publish(tmp_path / "mirror", "0.2.0")
    archive = release / "sil-native-dev-0.2.0-linux-x86_64.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    prefix = tmp_path / "sil"

    proc = install(host, "--version", "0.2.0", "--prefix", str(prefix))

    assert proc.returncode == 1
    assert "sil-native-dev-0.2.0-linux-x86_64.tar.gz does not match SHA256SUMS" in proc.stderr
    assert sorted(path.name for path in prefix.iterdir()) == []


def test_a_missing_release_installs_nothing(tmp_path, host):
    (tmp_path / "mirror").mkdir()
    prefix = tmp_path / "sil"

    proc = install(host, "--version", "9.9.9", "--prefix", str(prefix))

    assert proc.returncode == 1
    assert "could not download" in proc.stderr
    assert "v9.9.9/SHA256SUMS" in proc.stderr
    assert list(prefix.iterdir()) == []


@pytest.mark.parametrize("system, machine", [("Darwin", "arm64"), ("Linux", "aarch64")])
def test_an_unsupported_host_is_refused_before_any_download(tmp_path, host, system, machine):
    (Path(host["PATH"].split(os.pathsep)[0]) / "uname").write_text(
        f'#!/bin/sh\ncase "$1" in -s) echo {system} ;; -m) echo {machine} ;; esac\n')
    prefix = tmp_path / "sil"

    proc = install(host, "--version", "0.2.0", "--prefix", str(prefix))

    assert proc.returncode == 1
    assert f"Linux x86-64 only, not {system} {machine}" in proc.stderr
    assert "WSL2" in proc.stderr
    assert "runtime image" in proc.stderr
    assert "--platform linux/amd64" in proc.stderr
    assert not prefix.exists()


@needs_venv
def test_an_installed_version_is_kept_and_not_downloaded_again(tmp_path, host):
    release = publish(tmp_path / "mirror", "0.2.0")
    prefix = tmp_path / "sil"
    assert install(host, "--version", "0.2.0", "--prefix", str(prefix)).returncode == 0
    marker = prefix / "0.2.0" / "native" / "bin" / "sil-run"
    before = marker.stat().st_mtime_ns
    for path in release.iterdir():
        path.unlink()

    proc = install(host, "--version", "0.2.0", "--prefix", str(prefix))

    assert proc.returncode == 0, proc.stderr
    assert "already installed" in proc.stdout
    assert marker.stat().st_mtime_ns == before


@needs_venv
def test_versions_stay_side_by_side_and_the_last_installed_is_current(tmp_path, host):
    publish(tmp_path / "mirror", "0.2.0")
    publish(tmp_path / "mirror", "0.3.0")
    prefix = tmp_path / "sil"

    assert install(host, "--version", "0.3.0", "--prefix", str(prefix)).returncode == 0
    assert install(host, "--version", "0.2.0", "--prefix", str(prefix)).returncode == 0

    assert in_env(prefix, "sil-run --version", host) == "0.2.0"
    assert (prefix / "0.3.0" / ".complete").is_file()
    assert install(host, "--version", "0.3.0", "--prefix", str(prefix)).returncode == 0
    assert in_env(prefix, "sil-run --version", host) == "0.3.0"


def test_an_incomplete_installation_is_not_used(tmp_path, host):
    publish(tmp_path / "mirror", "0.2.0")
    prefix = tmp_path / "sil"
    (prefix / "0.2.0" / "venv").mkdir(parents=True)

    proc = install(host, "--version", "0.2.0", "--prefix", str(prefix))

    assert proc.returncode == 1
    assert "not a complete installation" in proc.stderr
    assert not (prefix / "current").exists()


@needs_venv
def test_a_runner_of_another_version_fails_the_installation(tmp_path, host):
    release = publish(tmp_path / "mirror", "0.2.0")
    (release / "sil-native-dev-0.2.0-linux-x86_64.tar.gz").write_bytes(
        _native_archive("0.1.0"))
    (release / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(release.iterdir()) if path.name != "SHA256SUMS"))
    prefix = tmp_path / "sil"

    proc = install(host, "--version", "0.2.0", "--prefix", str(prefix))

    assert proc.returncode == 1
    assert "reports version '0.1.0', not 0.2.0" in proc.stderr
    assert list(prefix.iterdir()) == []


@pytest.mark.parametrize("argv, message", [
    ((), "give the release with --version"),
    (("--version", "v0.2.0"), "is not a release version"),
    (("--version", "0.2"), "is not a release version"),
    (("--version", "0.2.0.1"), "is not a release version"),
    (("--version",), "--version needs a value"),
    (("--force",), "unknown argument '--force'"),
])
def test_usage_errors_exit_2(tmp_path, host, argv, message):
    proc = install(host, *argv, "--prefix", str(tmp_path / "sil")) \
        if argv[-1:] != ("--version",) else install(host, *argv)

    assert proc.returncode == 2
    assert message in proc.stderr
    assert "usage: install.sh" in proc.stderr


@needs_venv
def test_the_release_script_installs_its_own_version_by_default(tmp_path, host):
    version = project_version(ROOT)
    publish(tmp_path / "mirror", version)
    script = stamp_installer(ROOT, version, tmp_path / "install.sh")

    proc = install(host, "--prefix", str(tmp_path / "sil"), script=script)

    assert proc.returncode == 0, proc.stderr
    assert in_env(tmp_path / "sil", "sil-run --version", host) == version
    assert f"(default {version})" in install(host, "--help", script=script).stdout
    assert SCRIPT.read_text().count('DEFAULT_VERSION=""\n') == 1


def test_the_installer_is_stamped_only_with_the_project_version(tmp_path):
    with pytest.raises(ReleaseError, match="does not match project version"):
        stamp_installer(ROOT, "9.9.9", tmp_path / "install.sh")
