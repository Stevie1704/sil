"""The SiL agent skill: generated at release, installed by `sil skill install` (#277)."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT
from test_staged_install import installed_environment

sys.path.insert(0, str(ROOT / "tools"))

from release import PACKAGED_DOCS, stamp_python  # noqa: E402
from sil.cli import ROOT as CLI_ROOT, Group  # noqa: E402


def _command_paths(group: Group, path: str) -> list[str]:
    paths = []
    for name, entry in group.commands.items():
        if isinstance(entry, Group):
            paths += _command_paths(entry, f"{path} {name}")
        else:
            paths.append(f"{path} {name}")
    return paths


@pytest.fixture(scope="module")
def skill(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The SKILL.md of a freshly stamped Python tree."""
    destination = tmp_path_factory.mktemp("stamped") / "python-release"
    stamp_python(ROOT, "0.1.0", "test-revision", destination)
    return (destination / "src" / "sil" / "_skill" / "SKILL.md").read_text()


def _frontmatter(skill: str) -> dict[str, str]:
    head, _, _ = skill.removeprefix("---\n").partition("\n---\n")
    return dict(line.split(": ", 1) for line in head.splitlines()
                if ": " in line and not line.startswith(" "))


def test_the_skill_is_in_the_agent_skills_format(skill: str):
    assert skill.startswith("---\n")
    frontmatter = _frontmatter(skill)
    assert frontmatter["name"] == "sil"
    assert 0 < len(frontmatter["description"]) <= 1024
    for trigger in ("FMU", "Manifest", "Recording", "determinism", "Run fail"):
        assert trigger in frontmatter["description"]


def test_the_skill_states_the_version_it_was_generated_for(skill: str):
    assert '  sil-version: "0.1.0"\n' in skill
    assert "SiL 0.1.0" in skill.partition("\n---\n")[2]


def test_the_skill_names_every_command_of_sil(skill: str):
    commands = _command_paths(CLI_ROOT, "sil")
    assert "sil skill install" in commands
    for command in commands:
        assert f"| `{command}` |" in skill, command


def test_every_topic_the_skill_names_is_in_sil_docs(skill: str):
    named = set(re.findall(r"sil docs ([a-z][a-z-]*)", skill))
    assert {"agent-guide", "fmi", "running"} <= named
    assert named <= set(PACKAGED_DOCS)


def test_the_skill_does_not_copy_the_agent_guide(skill: str):
    guide = (ROOT / "docs" / "agent-guide.md").read_text()
    body = skill.partition("\n---\n")[2]
    copied = [line for line in body.splitlines()
              if len(line) > 40 and line in guide]
    assert copied == []


def _sil_skill(venv: Path, *argv: str, cwd: Path, home: Path) -> subprocess.CompletedProcess:
    """`sil skill install` of the installed wheel, away from the checkout."""
    env = installed_environment(venv)
    env["HOME"] = str(home)
    return subprocess.run([str(venv / "bin" / "sil"), "skill", "install", *argv],
                          cwd=cwd, env=env, capture_output=True, text=True)


def test_install_writes_the_skill_into_the_project_and_links_it_for_claude(
    installed_python: Path, tmp_path: Path,
):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    proc = _sil_skill(installed_python, cwd=project, home=home)

    assert proc.returncode == 0, proc.stderr
    written = project / ".agents" / "skills" / "sil" / "SKILL.md"
    assert proc.stdout.splitlines()[0] == str(written)
    assert '  sil-version: "0.1.0"\n' in written.read_text()
    link = project / ".claude" / "skills" / "sil"
    assert link.is_symlink()
    assert not Path(link.readlink()).is_absolute()
    assert (link / "SKILL.md").read_text() == written.read_text()
    assert not home.exists()


def test_install_replaces_an_older_copy(installed_python: Path, tmp_path: Path):
    skill = tmp_path / ".agents" / "skills" / "sil"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("an older version\n")
    first = _sil_skill(installed_python, cwd=tmp_path, home=tmp_path / "home")
    second = _sil_skill(installed_python, cwd=tmp_path, home=tmp_path / "home")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "sil-version" in (skill / "SKILL.md").read_text()


def test_install_dir_writes_only_into_that_directory(
    installed_python: Path, tmp_path: Path,
):
    proc = _sil_skill(installed_python, "--dir", "skills/sil", cwd=tmp_path,
                      home=tmp_path / "home")

    assert proc.returncode == 0, proc.stderr
    written = tmp_path / "skills" / "sil" / "SKILL.md"
    assert proc.stdout == f"{written}\n"
    assert written.is_file()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["skills"]


def test_install_user_writes_into_the_home_directory(
    installed_python: Path, tmp_path: Path,
):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    proc = _sil_skill(installed_python, "--user", cwd=project, home=home)

    assert proc.returncode == 0, proc.stderr
    written = home / ".agents" / "skills" / "sil" / "SKILL.md"
    assert proc.stdout.splitlines()[0] == str(written)
    assert (home / ".claude" / "skills" / "sil" / "SKILL.md").is_file()
    assert "pinned" in proc.stderr
    assert list(project.iterdir()) == []


def test_install_will_not_replace_a_claude_skill_that_is_not_a_link(
    installed_python: Path, tmp_path: Path,
):
    own = tmp_path / ".claude" / "skills" / "sil"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("the user's own skill\n")
    proc = _sil_skill(installed_python, cwd=tmp_path, home=tmp_path / "home")

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert str(own) in proc.stderr
    assert (own / "SKILL.md").read_text() == "the user's own skill\n"
    assert not (tmp_path / ".agents").exists()


def test_install_dir_and_user_exclude_each_other(installed_python: Path, tmp_path: Path):
    proc = _sil_skill(installed_python, "--user", "--dir", "x", cwd=tmp_path,
                      home=tmp_path / "home")

    assert proc.returncode == 2
    assert proc.stdout == ""


def test_a_checkout_has_no_skill_to_install(tmp_path: Path):
    """An unstamped checkout has no generated skill; a release has it."""
    proc = subprocess.run([sys.executable, "-m", "sil.skill"], cwd=tmp_path,
                          capture_output=True, text=True, check=False)

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "no skill" in proc.stderr
    assert list(tmp_path.iterdir()) == []


def test_the_wheel_holds_the_skill(wheel_path: Path):
    with zipfile.ZipFile(wheel_path) as wheel:
        skill = wheel.read("sil/_skill/SKILL.md").decode()

    assert skill.startswith("---\nname: sil\n")
