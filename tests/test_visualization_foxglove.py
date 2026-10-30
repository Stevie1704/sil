"""Served playback through the Foxglove SDK adapter (issue #349, spec #345
s3 "Headless playback and Foxglove adapter").

Two seams: the `sil playback serve` process, and the WebSocket wire protocol
that a client of the server sees. `FoxgloveClient` speaks that protocol
(subprotocol `foxglove.sdk.v1`) the way the Foxglove application does; no
Foxglove application runs here. Expected deliveries come from a sequential
scan of the source, never from the index or the controller under test.
"""

from __future__ import annotations

import os
import subprocess
import sys

from conftest import ROOT
from visualization_fixture import write_qualification_source

PYTHON_SRC = ROOT / "python" / "src"


def without_foxglove_sdk(tmp_path) -> dict:
    """An environment in which `import foxglove` fails, as it does when the
    `sil[foxglove]` extra is not installed."""
    blocker = tmp_path / "blocker" / "foxglove"
    blocker.mkdir(parents=True)
    (blocker / "__init__.py").write_text(
        "raise ImportError('foxglove-sdk is not installed')\n")
    path = os.pathsep.join([str(blocker.parent), str(PYTHON_SRC)])
    return dict(os.environ, PYTHONPATH=path)


def sil(args, env, **kwargs):
    return subprocess.run([sys.executable, "-m", "sil.cli", *args], env=env,
                          capture_output=True, text=True, timeout=120, **kwargs)


def test_serve_without_the_foxglove_extra_names_the_extra(tmp_path):
    source = write_qualification_source(tmp_path / "run.mcap")
    proc = sil(["playback", "serve", str(source), "--adapter", "foxglove",
                "--cache-dir", str(tmp_path / "cache")],
               without_foxglove_sdk(tmp_path))
    assert proc.returncode == 2, proc.stderr
    assert "pip install 'sil[foxglove]'" in proc.stderr


def test_export_and_other_commands_work_without_the_foxglove_extra(tmp_path):
    env = without_foxglove_sdk(tmp_path)
    source = write_qualification_source(tmp_path / "run.mcap")
    export = sil(["recording", "export", str(source), "--profile",
                  "visualization-json", "-o", str(tmp_path / "out.mcap")], env)
    assert export.returncode == 0, export.stderr
    assert (tmp_path / "out.mcap.receipt.json").exists()
    channels = sil(["recording", "channels", str(source)], env)
    assert channels.returncode == 0, channels.stderr
    help_text = sil(["playback", "serve", "--help"], env)
    assert help_text.returncode == 0, help_text.stderr
    assert "--listen" in help_text.stdout
