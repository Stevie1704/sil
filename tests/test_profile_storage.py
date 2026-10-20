"""The free-space reserve of a profile directory (#313).

A collector stops its raw data before the file system of the profile
directory is full, so the Run goes on and the reports can be written. These
tests use the real free space of the test directory and move the reserve
around it.
"""

from __future__ import annotations

import os

from sil import profile_storage


def free(path) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def test_the_reserve_is_reached_when_free_space_is_below_it(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(profile_storage, "RESERVE_BYTES", 1 << 62)

    assert profile_storage.reserve_reached(tmp_path)


def test_the_reserve_is_not_reached_with_space_above_it(tmp_path,
                                                        monkeypatch):
    monkeypatch.setattr(profile_storage, "RESERVE_BYTES", 0)

    assert not profile_storage.reserve_reached(tmp_path)


def test_a_directory_without_free_space_facts_is_not_stopped(tmp_path):
    assert not profile_storage.reserve_reached(tmp_path / "missing")


def test_a_capture_limit_above_the_free_space_gets_a_warning(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(profile_storage, "RESERVE_BYTES", 0)

    warning = profile_storage.space_warning(tmp_path, free(tmp_path) * 2)

    assert warning is not None
    assert "bytes free" in warning and "reserve" in warning


def test_a_capture_limit_within_the_free_space_gets_no_warning(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(profile_storage, "RESERVE_BYTES", 0)

    assert profile_storage.space_warning(tmp_path, 1) is None
