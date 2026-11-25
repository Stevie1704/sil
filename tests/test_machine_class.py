"""The Linux machine class of the host."""

from __future__ import annotations

import platform

import pytest

from sil.machine_class import other_linux_machine_class


@pytest.mark.parametrize(("system", "machine", "expected"), [
    ("Linux", "x86_64", None),
    ("Linux", "aarch64", "aarch64"),
    ("Darwin", "arm64", None),
    ("Darwin", "x86_64", None),
])
def test_only_a_linux_host_that_is_not_x86_64_is_another_machine_class(
    monkeypatch, system, machine, expected,
):
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)

    assert other_linux_machine_class() == expected
