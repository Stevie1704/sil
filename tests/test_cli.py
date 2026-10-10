"""The `sil` command: one entry point that names and dispatches every tool (#266)."""

import os
import subprocess
import sys

import pytest

from sil import cli


def test_help_lists_every_top_level_command(capsys):
    assert cli.main(["--help"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("usage: sil <command>")
    for command in ("check", "compare", "footprint", "run",
                    "bundle", "fmi", "recording", "schema"):
        assert f"\n  {command} " in out


def test_a_subcommand_runs_with_its_own_arguments_and_exit_code(tmp_path, capsys):
    missing = tmp_path / "missing.json"

    assert cli.main(["footprint", str(missing)]) == 2

    assert capsys.readouterr().err.startswith(
        "sil footprint: cannot read manifest: ")


@pytest.mark.parametrize("group, commands", [
    ("bundle", ("seal", "verify", "run", "matrix")),
    ("fmi", ("inspect", "couple", "replay", "substitute")),
    ("recording", ("csv", "window")),
    ("schema", ("import",)),
])
def test_group_help_lists_its_subcommands(group, commands, capsys):
    assert cli.main([group, "--help"]) == 0

    out = capsys.readouterr().out
    assert out.startswith(f"usage: sil {group} <command>")
    for command in commands:
        assert f"\n  {command} " in out


def test_a_bundle_subcommand_reaches_the_bundle_tool(tmp_path, capsys):
    assert cli.main(["bundle", "verify", str(tmp_path)]) == 2

    assert capsys.readouterr().err.startswith("sil bundle: refused: ")


@pytest.mark.parametrize("argv, problem", [
    ([], "sil: error: a command is required"),
    (["sil-check"], "sil: error: unknown command 'sil-check'"),
    (["fmi", "inspekt"], "sil fmi: error: unknown command 'inspekt'"),
])
def test_a_missing_or_unknown_command_is_a_usage_error(argv, problem, capsys):
    assert cli.main(argv) == 2

    err = capsys.readouterr().err
    assert err.startswith("usage: ")
    assert err.endswith(problem + "\n")


def _sil(argv, path):
    """`sil` as its own process, with only `path` to search for executables."""
    return subprocess.run([sys.executable, "-m", "sil.cli", *argv],
                          capture_output=True, text=True,
                          env={**os.environ, "PATH": str(path)})


def test_run_hands_the_process_to_the_native_runner(tmp_path):
    runner = tmp_path / "sil-run"
    runner.write_text('#!/bin/sh\necho "runner got: $*"\nexit 3\n')
    runner.chmod(0o755)

    proc = _sil(["run", "manifest.json", "-o", "run.mcap", "--help"], tmp_path)

    assert proc.returncode == 3
    assert proc.stdout == "runner got: manifest.json -o run.mcap --help\n"


def test_run_without_a_native_runner_reports_it_as_not_found(tmp_path):
    proc = _sil(["run", "manifest.json"], tmp_path)

    assert proc.returncode == 127
    assert proc.stderr == "sil run: error: cannot find sil-run on PATH\n"
