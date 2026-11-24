"""The FMU binary directory the Importer selects on each machine class (#409).

The host is injected through `platform.system` and `platform.machine`, so
the Linux arm64, Linux x86-64 and Mac answers are checked on every host. The
one Run of an `aarch64-linux` binary needs an arm64 Linux host.
"""

from __future__ import annotations

import platform
import shutil
import zipfile
from pathlib import Path

import pytest
import test_fmi as fmi3
from conftest import ROOT
from sil.fmi import ModelDescription, platform_directory
from sil.fmi.description import fmi2_platform_directory
from sil.fmi.inspection import inspect
from sil.participant import ManifestError
from sil.testing import run_simulation

FMI3_FEEDTHROUGH = fmi3.FEEDTHROUGH
FMI2_DAHLQUIST = ROOT / "tests" / "fixtures" / "reference-fmus" / "2.0" / "Dahlquist.fmu"

LINUX_AARCH64 = ("Linux", "aarch64")


def host(monkeypatch, system: str, machine: str) -> None:
    """Make the Importer see `system` `machine` as the running host."""
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)


def extracted(tmp_path: Path, archive: Path) -> Path:
    with zipfile.ZipFile(archive) as opened:
        opened.extractall(tmp_path / "extracted")
    return tmp_path / "extracted"


class TestDirectoryPerMachineClass:
    @pytest.mark.parametrize(
        ("system", "machine", "fmi3_directory", "fmi2_directory"),
        [
            ("Linux", "aarch64", "aarch64-linux", None),
            ("Linux", "x86_64", "x86_64-linux", "linux64"),
            ("Darwin", "arm64", "aarch64-darwin", "darwin64"),
        ],
    )
    def test_each_fmi_version_names_its_own_directory(
        self, monkeypatch, system, machine, fmi3_directory, fmi2_directory
    ):
        host(monkeypatch, system, machine)
        assert platform_directory() == fmi3_directory
        assert fmi2_platform_directory() == fmi2_directory


class TestLinuxArm64Refusals:
    def test_fmi2_names_the_host_and_the_accepted_directories(
        self, monkeypatch, tmp_path
    ):
        files = extracted(tmp_path, FMI2_DAHLQUIST)
        host(monkeypatch, *LINUX_AARCH64)

        with pytest.raises(ManifestError) as refused:
            ModelDescription.read(files).binary(files)

        message = str(refused.value)
        assert "FMI 2.0" in message
        assert "(Linux aarch64)" in message
        assert "binaries/linux64 (Linux x86_64)" in message
        assert "binaries/darwin64 (Darwin x86_64 or Darwin arm64)" in message
        assert "binaries/aarch64-linux" in message

    def test_fmi2_inspection_names_no_directory_the_host_cannot_load(
        self, monkeypatch
    ):
        host(monkeypatch, *LINUX_AARCH64)

        report = inspect(FMI2_DAHLQUIST)

        assert report["verdict"] == "unusable"
        assert report["platform"] is None
        assert not any("linux64" in line for line in report["unverified"])

    def test_fmi3_with_only_x86_64_binaries_names_the_missing_directory(
        self, monkeypatch, tmp_path
    ):
        files = extracted(tmp_path, FMI3_FEEDTHROUGH)
        shutil.rmtree(files / "binaries" / "aarch64-linux")
        assert (files / "binaries" / "x86_64-linux").is_dir()
        host(monkeypatch, *LINUX_AARCH64)

        with pytest.raises(ManifestError, match=r"binaries/aarch64-linux/"):
            ModelDescription.read(files).binary(files)


@pytest.mark.skipif(
    (platform.system(), platform.machine()) != LINUX_AARCH64,
    reason="runs an aarch64-linux FMU binary, so needs a Linux arm64 host",
)
class TestOnLinuxArm64:
    def test_the_fmi3_feedthrough_loads_its_aarch64_linux_binary(
        self, tmp_path
    ):
        files = extracted(tmp_path, FMI3_FEEDTHROUGH)
        binary = ModelDescription.read(files).binary(files)
        assert binary.parent == files / "binaries" / "aarch64-linux"

    def test_a_run_of_the_fmi3_feedthrough_completes(
        self, sil_run, tmp_path
    ):
        result = run_simulation(
            fmi3.fmu_manifest(), runner=sil_run, workdir=tmp_path
        )
        assert len(result.messages("fmu.Out")) == (
            fmi3.DURATION_NS // fmi3.STEP_PERIOD_NS
        )
