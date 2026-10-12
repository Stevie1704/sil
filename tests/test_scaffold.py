"""`sil new participant`: a Participant that runs with no edits (#275)."""

import os
import subprocess
import sys

import pytest

from sil import check, manifest_validate, scaffold


def _build_manifest(directory, sil_run):
    """Run the generated builder as a user would, with sil-run on PATH."""
    env = {**os.environ,
           "PATH": f"{sil_run.parent}{os.pathsep}{os.environ['PATH']}"}
    manifest = directory / "manifest.json"
    subprocess.run([sys.executable, str(directory / "manifest.py"),
                    str(manifest)], check=True, env=env, capture_output=True)
    return manifest


@pytest.mark.parametrize("kind, files", [
    ("--process", {"participant.py", "manifest.py"}),
    ("--native", {"participant.c", "manifest.py"}),
])
def test_the_generated_participant_passes_validate_and_check(
        kind, files, sil_run, tmp_path, capsys):
    directory = tmp_path / "brake"

    assert scaffold.main([kind, "brake", "--dir", str(directory)]) == 0

    assert {path.name for path in directory.iterdir()} == files
    manifest = _build_manifest(directory, sil_run)
    assert manifest_validate.validate(manifest) == []
    capsys.readouterr()
    assert check.check(sil_run, manifest) == 0


def test_the_participant_is_written_to_a_directory_named_after_it(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert scaffold.main(["--process", "brake"]) == 0

    assert (tmp_path / "brake" / "participant.py").is_file()


def test_existing_files_are_not_overwritten(tmp_path, capsys):
    (tmp_path / "manifest.py").write_text("mine\n")

    assert scaffold.main(["--process", "brake", "--dir", str(tmp_path)]) == 2

    assert (tmp_path / "manifest.py").read_text() == "mine\n"
    assert not (tmp_path / "participant.py").exists()
    assert "manifest.py" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["brake-ecu", "1st", "int", "speed_t", ""])
def test_a_name_that_is_not_a_c_identifier_is_refused(name, tmp_path, capsys):
    assert scaffold.main(["--process", name, "--dir", str(tmp_path)]) == 2

    assert list(tmp_path.iterdir()) == []
    assert repr(name) in capsys.readouterr().err
