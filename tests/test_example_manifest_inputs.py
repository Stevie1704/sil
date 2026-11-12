"""An example refuses a repeated key in the JSON it writes into its Manifest.

Each example reads Schemas or a mapping from a JSON file and puts them into
the Manifest it writes. A repeated key in that file would be lost before the
Manifest exists, so the kernel could not see it (#394). Each test starts the
example from a copy whose file repeats a key, as a user's copy could.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

REPEAT = "object '' repeats key 'x'"


def _repeat_a_key(path: Path) -> None:
    path.write_text(path.read_text().replace("{", '{"x": 0, "x": 0, ', 1))


def _start(script: Path, out: Path, pythonpath: Path, *argv):
    env = {**os.environ, "PYTHONPATH": str(pythonpath)}
    return subprocess.run([sys.executable, *map(str, argv), str(out)],
                          capture_output=True, text=True, env=env,
                          cwd=script.parent)


@pytest.mark.parametrize("example, script, data", [
    ("fmu", "manifest.py", "schemas.json"),
    ("adas-reference", "manifest.py", "schemas.json"),
    ("library", "manifest.py", "mapping.json"),
    ("library", "gap_manifest.py", "gap_mapping.json"),
    ("csv", "manifest.py", "mapping.json"),
], ids=["fmu", "adas", "library", "library-gap", "csv"])
def test_an_example_refuses_a_repeated_key_before_its_manifest(
        tmp_path, example, script, data):
    copy = tmp_path / "examples" / example
    shutil.copytree(ROOT / "examples" / example, copy,
                    ignore=shutil.ignore_patterns("__pycache__"))
    _repeat_a_key(copy / data)
    out = tmp_path / "out.json"

    proc = _start(copy / script, out, ROOT / "python" / "src", copy / script)

    assert proc.returncode != 0
    assert REPEAT in proc.stderr
    assert not out.exists()


def test_the_acc_example_refuses_a_repeated_key_before_its_manifest(tmp_path):
    package = tmp_path / "src" / "sil"
    shutil.copytree(ROOT / "python" / "src" / "sil", package,
                    ignore=shutil.ignore_patterns("__pycache__"))
    _repeat_a_key(package / "examples" / "acc" / "acc.json")
    out = tmp_path / "out.json"

    proc = _start(package / "examples" / "acc" / "manifest.py", out,
                  package.parent, "-m", "sil.examples.acc.manifest")

    assert proc.returncode != 0
    assert REPEAT in proc.stderr
    assert not out.exists()
